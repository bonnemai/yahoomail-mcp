import time

from botocore.exceptions import ClientError

from yahoomail_mcp.approval import DynamoApprovalStore


class FakeTable:
    def __init__(self):
        self.item = None

    def put_item(self, Item):
        self.item = Item

    def get_item(self, Key, ConsistentRead=True):
        return {"Item": self.item} if self.item and self.item["approval_id"] == Key["approval_id"] else {}

    def delete_item(self, **kwargs):
        item = self.item
        token = kwargs["ExpressionAttributeValues"][":token"]
        now = kwargs["ExpressionAttributeValues"][":now"]
        if not item or item["approval_token"] != token or item["expires_at"] <= now:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "DeleteItem",
            )
        self.item = None
        return {"Attributes": item}


def test_dynamo_approval_store_creates_reads_and_consumes_once():
    table = FakeTable()
    store = DynamoApprovalStore(table)
    store.create("preview", "token", {"uids": [1]}, int(time.time()) + 60)
    assert store.get("preview", "wrong") is None
    assert store.get("preview", "token")["uids"] == [1]
    assert store.consume("preview", "token")["uids"] == [1]
    assert store.consume("preview", "token") is None


def test_dynamo_approval_store_hides_expired_records():
    table = FakeTable()
    store = DynamoApprovalStore(table)
    store.create("preview", "token", {}, int(time.time()) - 1)
    assert store.get("preview", "token") is None
    assert store.consume("preview", "token") is None
