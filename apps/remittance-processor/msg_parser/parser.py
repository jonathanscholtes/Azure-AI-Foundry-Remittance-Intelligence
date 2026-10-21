from __future__ import annotations

import mimetypes
import os
import tempfile
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path, PurePath
from typing import Any


@dataclass(frozen=True)
class ParsedAttachment:
    name: str
    extension: str
    content_type: str
    content: bytes
    is_inline: bool
    # Lineage for content unpacked from an embedded (forwarded / attached) email: the chain of
    # embedded message subjects, outermost first. None for the message's own attachments.
    embedded_in: str | None = None
    source_type: str = "attachment"  # or "embedded_message_body"
    detected_type: str | None = None  # extension inferred from content when the name had none


# File types recognized from content when an attachment has no or an unknown extension (e.g. a
# scanned "WIRE_TRANSFER_3690_..." that is really a TIFF). Signatures are the formats' own magic bytes.
KNOWN_EXTENSIONS = {
    ".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".heic", ".heif",
    ".xlsx", ".xlsm", ".xls", ".csv", ".docx", ".docm", ".doc", ".txt", ".rtf", ".odt", ".ods", ".msg", ".eml", ".zip",
}
_SIGNATURES = (
    (b"%PDF", ".pdf", "application/pdf"),
    (b"II*\x00", ".tif", "image/tiff"),
    (b"MM\x00*", ".tif", "image/tiff"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"{\\rtf", ".rtf", "application/rtf"),
)


def detect_type(content: bytes) -> tuple[str, str] | None:
    """(extension, content type) from the file's own signature, or None if unrecognized."""
    head = content[:16]
    for signature, extension, content_type in _SIGNATURES:
        if head.startswith(signature):
            return extension, content_type
    if head.startswith(b"PK\x03\x04"):  # ZIP container: Office Open XML or a plain archive
        import io
        import zipfile

        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except zipfile.BadZipFile:
            return None
        if any(name.startswith("xl/") for name in names):
            return ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if any(name.startswith("word/") for name in names):
            return ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return ".zip", "application/zip"
    return None


def typed_attachment(name: str, content: bytes, content_type: str | None, **fields) -> ParsedAttachment:
    """A ParsedAttachment whose extension and content type come from its content when the name
    carries no known extension. The original name is kept; ``detected_type`` records the inference."""
    extension = Path(name).suffix.lower()
    detected = None
    if extension not in KNOWN_EXTENSIONS:
        found = detect_type(content)
        if found:
            extension, content_type = found
            detected = found[0]
    return ParsedAttachment(
        name=name,
        extension=extension,
        content_type=content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
        content=content,
        detected_type=detected,
        **fields,
    )


# Payers often attach the original remittance email ("see attached email") instead of forwarding it.
# Embedded messages are unpacked recursively up to this depth; deeper nesting is flagged.
MAX_EMBED_DEPTH = 4


def embedded_body_attachment(*, subject, sender, sent_date, body, chain: str, index: int) -> ParsedAttachment:
    """An embedded email's own text, as a plain-text source with its headers for context."""
    header = "\n".join(
        f"{label}: {value}" for label, value in (("Subject", subject), ("From", sender), ("Date", sent_date)) if value
    )
    text = f"{header}\n\n{body}".strip()
    return ParsedAttachment(
        name=f"embedded-email-{index}.txt",
        extension=".txt",
        content_type="text/plain",
        content=text.encode("utf-8"),
        is_inline=False,
        embedded_in=chain,
        source_type="embedded_message_body",
    )


@dataclass(frozen=True)
class ParsedMessage:
    subject: str | None
    sender_name: str | None
    sender_email: str | None
    sent_date: str | None
    body: str | None
    html_body: str | None
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    attachments: list[ParsedAttachment]
    warnings: list[str]


def _safe_name(value: str | None, index: int) -> str:
    if value:
        name = PurePath(value.replace("\\", "/")).name
        name = "".join(char if char.isalnum() or char in " ._-()" else "_" for char in name)
        name = name.strip(" .")
        if name:
            return name[:255]
    return f"attachment-{index}.bin"


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _html_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _sender_email(message: Any) -> str | None:
    for value in (getattr(message, "senderEmail", None), getattr(message, "sender", None)):
        if value:
            _, address = parseaddr(str(value))
            if address:
                return address
    return None


def _header_value(message: Any, name: str) -> str | None:
    headers = getattr(message, "headerDict", None) or {}
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value).strip() if value is not None else None
    return None


def _message_references(message: Any) -> list[str]:
    value = _header_value(message, "References")
    return value.split() if value else []


def _collect_msg_attachments(message, attachments: list, warnings: list, *, chain: str | None, depth: int) -> None:
    """Append the message's file attachments; unpack embedded emails (body + their attachments)."""
    for index, attachment in enumerate(getattr(message, "attachments", None) or [], start=1):
        data = getattr(attachment, "data", None)
        original_name = (
            getattr(attachment, "longFilename", None)
            or getattr(attachment, "shortFilename", None)
            or getattr(attachment, "displayName", None)
        )
        if data is not None and not isinstance(data, (bytes, bytearray)) and hasattr(data, "attachments"):
            # Embedded email (extract_msg returns a Message). Its body is read as plain text only -
            # never htmlBody, which would de-encapsulate RTF (minutes on large remittances).
            subject = getattr(data, "subject", None) or original_name or f"embedded email {index}"
            inner_chain = f"{chain} > {subject}" if chain else str(subject)
            if depth + 1 > MAX_EMBED_DEPTH:
                warnings.append(f"embedded email nested deeper than {MAX_EMBED_DEPTH} levels skipped: {inner_chain}")
                continue
            body = getattr(data, "body", None)
            if body and str(body).strip():
                sender = getattr(data, "sender", None)
                attachments.append(
                    embedded_body_attachment(
                        subject=subject, sender=str(sender) if sender else None, sent_date=_as_iso(getattr(data, "date", None)),
                        body=str(body), chain=inner_chain, index=len(attachments) + 1,
                    )
                )
            _collect_msg_attachments(data, attachments, warnings, chain=inner_chain, depth=depth + 1)
            continue
        if not isinstance(data, (bytes, bytearray)):
            where = f" in {chain}" if chain else ""
            warnings.append(f"attachment {index}{where} skipped: no file data")
            continue

        name = _safe_name(original_name, len(attachments) + 1)
        is_inline = any(
            getattr(attachment, attribute, False) is True
            for attribute in ("hidden", "isInline", "inline")
        ) or bool(getattr(attachment, "cid", None) or getattr(attachment, "contentId", None))
        attachments.append(
            typed_attachment(
                name,
                bytes(data),
                getattr(attachment, "mimetype", None) or mimetypes.guess_type(name)[0],
                is_inline=is_inline,
                embedded_in=chain,
            )
        )


def parse_msg(content: bytes, filename: str = "message.msg") -> ParsedMessage:
    """Decompose an Outlook MSG without interpreting remittance business fields."""
    if not content:
        raise ValueError("MSG content must not be empty")

    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("Install the extract-msg package to parse Outlook MSG files") from exc

    safe_filename = _safe_name(filename, 0)
    temp_dir = tempfile.TemporaryDirectory(prefix="remittance-msg-")
    message = None
    try:
        temp_path = os.path.join(temp_dir.name, safe_filename)
        with open(temp_path, "wb") as output:
            output.write(content)

        try:
            message = extract_msg.openMsg(temp_path)
        except AttributeError:
            message = extract_msg.Message(temp_path)

        warnings: list[str] = []
        attachments: list[ParsedAttachment] = []
        _collect_msg_attachments(message, attachments, warnings, chain=None, depth=0)

        sender = getattr(message, "sender", None)
        body = getattr(message, "body", None)
        # Accessing message.htmlBody forces extract_msg to deencapsulate the compressed RTF body into
        # HTML, which is very slow (seconds to minutes) for large remittance tables. Only pay that cost
        # when there is no usable plain-text body - extraction runs on the plain body, and the HTML is
        # only a convenience artifact. Guard the fallback so a malformed RTF cannot fail intake.
        html_body = None
        if not (body and str(body).strip()):
            try:
                html_body = _html_text(getattr(message, "htmlBody", None))
            except Exception:
                warnings.append("html_body_deencapsulation_failed")
        return ParsedMessage(
            subject=getattr(message, "subject", None),
            sender_name=str(sender) if sender is not None else None,
            sender_email=_sender_email(message),
            sent_date=_as_iso(getattr(message, "date", None)),
            body=body,
            html_body=html_body,
            message_id=getattr(message, "messageId", None) or _header_value(message, "Message-ID"),
            in_reply_to=getattr(message, "inReplyTo", None) or _header_value(message, "In-Reply-To"),
            references=_message_references(message),
            attachments=attachments,
            warnings=warnings,
        )
    finally:
        close = getattr(message, "close", None)
        if callable(close):
            close()
        temp_dir.cleanup()