from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from katcha.integrations.storage import ObjectStore


class FakeS3:
    def __init__(self, *, status: int, code: str) -> None:
        self.status = status
        self.code = code
        self.created: list[dict[str, object]] = []

    def head_bucket(self, **_kwargs):
        raise ClientError(
            {
                "Error": {"Code": self.code, "Message": "test"},
                "ResponseMetadata": {"HTTPStatusCode": self.status},
            },
            "HeadBucket",
        )

    def create_bucket(self, **kwargs):
        self.created.append(kwargs)


def store_with(client: FakeS3, *, region: str = "us-east-1") -> ObjectStore:
    store = object.__new__(ObjectStore)
    store.client = client
    store.settings = SimpleNamespace(s3_bucket="katcha-media-test", s3_region=region)
    return store


def test_ensure_bucket_creates_only_on_not_found():
    client = FakeS3(status=404, code="NoSuchBucket")
    store_with(client, region="us-west-2").ensure_bucket()

    assert client.created == [
        {
            "Bucket": "katcha-media-test",
            "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
        }
    ]


def test_ensure_bucket_does_not_turn_access_denied_into_create():
    client = FakeS3(status=403, code="AccessDenied")

    with pytest.raises(ClientError):
        store_with(client).ensure_bucket()

    assert client.created == []
