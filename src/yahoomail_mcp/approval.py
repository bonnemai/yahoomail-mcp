"""Durable, expiring approval storage for the remote Lambda deployment."""
import time


class DynamoApprovalStore:
    def __init__(self, table):
        self.table = table

    def create(self, approval_id, approval_token, payload, expires_at):
        item = {
            "approval_id": approval_id,
            "approval_token": approval_token,
            "expires_at": expires_at,
            **payload,
        }
        self.table.put_item(Item=item)

    def get(self, approval_id, approval_token):
        response = self.table.get_item(Key={"approval_id": approval_id}, ConsistentRead=True)
        item = response.get("Item")
        if not item or item.get("approval_token") != approval_token:
            return None
        if int(item.get("expires_at", 0)) <= int(time.time()):
            return None
        return item

    def consume(self, approval_id, approval_token):
        from botocore.exceptions import ClientError

        try:
            response = self.table.delete_item(
                Key={"approval_id": approval_id},
                ConditionExpression="approval_token = :token AND expires_at > :now",
                ExpressionAttributeValues={
                    ":token": approval_token,
                    ":now": int(time.time()),
                },
                ReturnValues="ALL_OLD",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        item = response.get("Attributes")
        return item if item and item.get("approval_token") == approval_token else None
