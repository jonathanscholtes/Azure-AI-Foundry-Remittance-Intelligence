import os

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceExistsError


def main() -> None:
    connection_string = os.environ.get(
        "STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true"
    )
    service = BlobServiceClient.from_connection_string(connection_string)
    container_names = (
        "incoming",
        "attachments",
        "candidates",
        "bodies",
        "analysis",
        "audit",
        "errors",
        "manifests",
        "results",
    )
    for container_name in container_names:
        try:
            service.create_container(container_name)
        except ResourceExistsError:
            pass

    from azure.data.tables import TableServiceClient

    tables = TableServiceClient.from_connection_string(
        os.environ.get("STORAGE_TABLE_CONNECTION_STRING", connection_string)
    )
    for table_name in ("remittances", "remittanceevents"):
        tables.create_table_if_not_exists(table_name)
    print("Local remittance containers and ledger tables are ready.")


if __name__ == "__main__":
    main()