"""RFC 822 / MIME (.eml) intake for the email stream and the synthetic load generator.

Produces the same ``ParsedMessage`` shape as ``parse_msg`` so everything downstream (candidate
selection, extraction, validation) is identical regardless of the source format.
"""

from __future__ import annotations

import mimetypes
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from msg_parser.parser import (
    MAX_EMBED_DEPTH,
    ParsedMessage,
    _safe_name,
    embedded_body_attachment,
    typed_attachment,
)
from msg_parser.text_normalizer import html_to_plain_text


def _collect_parts(message, attachments: list, warnings: list, *, chain: str | None, depth: int):
    """Return this message's (text body, html body) and append its attachments. An attached email
    (message/rfc822) is unpacked: its body becomes a text source and its attachments are collected."""
    body = None
    html_body = None
    parts = [message] if not message.is_multipart() else list(message.iter_parts())
    while parts:
        part = parts.pop(0)
        content_type = part.get_content_type()
        if content_type == "message/rfc822":
            inner = part.get_payload(0) if isinstance(part.get_payload(), list) else part.get_payload()
            subject = str(inner.get("Subject") or part.get_filename() or "embedded email")
            inner_chain = f"{chain} > {subject}" if chain else subject
            if depth + 1 > MAX_EMBED_DEPTH:
                warnings.append(f"embedded email nested deeper than {MAX_EMBED_DEPTH} levels skipped: {inner_chain}")
                continue
            inner_body, inner_html = _collect_parts(inner, attachments, warnings, chain=inner_chain, depth=depth + 1)
            text = inner_body or (html_to_plain_text(inner_html) if inner_html else "")
            if text.strip():
                attachments.append(
                    embedded_body_attachment(
                        subject=subject, sender=inner.get("From"), sent_date=inner.get("Date"),
                        body=text, chain=inner_chain, index=len(attachments) + 1,
                    )
                )
            continue
        if part.is_multipart():
            parts[0:0] = list(part.iter_parts())
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename_value = part.get_filename()
        is_attachment = disposition in ("attachment", "inline") and bool(filename_value)

        if not is_attachment and content_type == "text/plain" and body is None:
            body = part.get_content()
            continue
        if not is_attachment and content_type == "text/html" and html_body is None:
            html_body = part.get_content()
            continue
        if not filename_value and disposition != "attachment":
            continue

        data = part.get_payload(decode=True)
        if not isinstance(data, (bytes, bytearray)):
            where = f" in {chain}" if chain else ""
            warnings.append(f"attachment {len(attachments) + 1}{where} skipped: no file data")
            continue
        name = _safe_name(filename_value, len(attachments) + 1)
        attachments.append(
            typed_attachment(
                name,
                bytes(data),
                content_type or mimetypes.guess_type(name)[0],
                is_inline=disposition == "inline" or bool(part.get("Content-ID")),
                embedded_in=chain,
            )
        )
    return body, html_body


def parse_eml(content: bytes, filename: str = "message.eml") -> ParsedMessage:
    if not content:
        raise ValueError("EML content must not be empty")

    message = BytesParser(policy=policy.default).parsebytes(content)
    warnings: list[str] = []
    attachments: list[ParsedAttachment] = []
    body, html_body = _collect_parts(message, attachments, warnings, chain=None, depth=0)

    sender_name, sender_email = parseaddr(str(message.get("From") or ""))
    sent_date = None
    if message.get("Date"):
        try:
            sent_date = parsedate_to_datetime(str(message.get("Date"))).isoformat()
        except (TypeError, ValueError):
            sent_date = str(message.get("Date"))
    references = str(message.get("References") or "").split()

    return ParsedMessage(
        subject=str(message.get("Subject")) if message.get("Subject") is not None else None,
        sender_name=sender_name or None,
        sender_email=sender_email or None,
        sent_date=sent_date,
        body=body,
        html_body=html_body,
        message_id=str(message.get("Message-ID")) if message.get("Message-ID") else None,
        in_reply_to=str(message.get("In-Reply-To")) if message.get("In-Reply-To") else None,
        references=references,
        attachments=attachments,
        warnings=warnings,
    )


def parse_message(content: bytes, filename: str):
    """Dispatch on extension: Outlook .msg or MIME .eml."""
    if filename.lower().endswith(".eml"):
        return parse_eml(content, filename)
    from msg_parser.parser import parse_msg

    return parse_msg(content, filename)
