import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings


class StorageError(Exception):
    pass


class StorageConfigurationError(StorageError):
    pass


@dataclass(frozen=True)
class UploadResult:
    size: int
    checksum: str


class StorageProvider(ABC):
    name = ""

    @abstractmethod
    def upload(self, key, content, *, content_type, metadata=None):
        raise NotImplementedError

    @abstractmethod
    def download(self, key):
        raise NotImplementedError

    @abstractmethod
    def delete(self, key):
        raise NotImplementedError

    @abstractmethod
    def exists(self, key):
        raise NotImplementedError

    @abstractmethod
    def get_metadata(self, key):
        raise NotImplementedError

    @abstractmethod
    def copy(self, source_key, target_key, *, source_bucket=None, metadata=None):
        raise NotImplementedError

    def checksum(self, key):
        digest = hashlib.sha256()
        with self.download(key) as content:
            for chunk in iter(lambda: content.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def generate_signed_upload_url(
        self, key, *, content_type, expires_in, content_length=None
    ):
        return None

    def generate_signed_download_url(self, key, *, expires_in, filename=None):
        return None

    def check_bucket(self):
        return True


class LocalProvider(StorageProvider):
    name = "local"

    def __init__(self):
        self.root = Path(settings.STORAGE_LOCAL_ROOT).resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError("Local storage could not be initialized.") from error

    def _path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("Invalid storage key.")
        return path

    def upload(self, key, content, *, content_type, metadata=None):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as target:
                for chunk in content.chunks() if hasattr(content, "chunks") else iter(lambda: content.read(1024 * 1024), b""):
                    if not chunk:
                        continue
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as error:
            raise StorageError("Local storage operation failed.") from error
        return UploadResult(size=size, checksum=digest.hexdigest())

    def download(self, key):
        try:
            return self._path(key).open("rb")
        except OSError as error:
            raise StorageError("Stored file is unavailable.") from error

    def delete(self, key):
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            raise StorageError("Local storage operation failed.") from error

    def exists(self, key):
        return self._path(key).is_file()

    def get_metadata(self, key):
        path = self._path(key)
        try:
            stat = path.stat()
        except OSError as error:
            raise StorageError("Stored file is unavailable.") from error
        return {"size": stat.st_size}

    def copy(self, source_key, target_key, *, source_bucket=None, metadata=None):
        with self.download(source_key) as source:
            return self.upload(
                target_key,
                source,
                content_type=(metadata or {}).get("content_type", "application/octet-stream"),
                metadata=metadata,
            )

    def generate_signed_upload_url(
        self, key, *, content_type, expires_in, content_length=None
    ):
        return None

    def generate_signed_download_url(self, key, *, expires_in, filename=None):
        return None


class TigrisProvider(StorageProvider):
    name = "tigris"

    def __init__(self):
        access_key = settings.TIGRIS_ACCESS_KEY_ID
        secret_key = settings.TIGRIS_SECRET_ACCESS_KEY
        if not access_key or not secret_key:
            raise StorageConfigurationError("Tigris credentials are not configured.")
        if not settings.TIGRIS_BUCKET:
            raise StorageConfigurationError("Tigris bucket is not configured.")
        endpoint = urlparse(settings.TIGRIS_ENDPOINT)
        if endpoint.scheme != "https" or not endpoint.netloc:
            raise StorageConfigurationError("Tigris endpoint configuration is invalid.")
        self.bucket = settings.TIGRIS_BUCKET
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=settings.TIGRIS_ENDPOINT,
                region_name=settings.TIGRIS_REGION,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            raise StorageConfigurationError(
                "Tigris provider could not be initialized."
            ) from error

    def _request(self, action):
        try:
            return action()
        except (BotoCoreError, ClientError, S3UploadFailedError, OSError) as error:
            raise StorageError("Tigris storage operation failed.") from error

    def upload(self, key, content, *, content_type, metadata=None):
        self._request(
            lambda: self.client.upload_fileobj(
                content,
                self.bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        str(name): str(value)
                        for name, value in (metadata or {}).items()
                        if len(str(name)) <= 64 and len(str(value)) <= 256
                    },
                },
            )
        )
        info = self.get_metadata(key)
        return UploadResult(size=info["size"], checksum=self.checksum(key))

    def download(self, key):
        response = self._request(lambda: self.client.get_object(Bucket=self.bucket, Key=key))
        return response["Body"]

    def delete(self, key):
        self._request(lambda: self.client.delete_object(Bucket=self.bucket, Key=key))

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise StorageError("Tigris storage operation failed.") from error
        except (BotoCoreError, OSError) as error:
            raise StorageError("Tigris storage operation failed.") from error

    def get_metadata(self, key):
        result = self._request(
            lambda: self.client.head_object(Bucket=self.bucket, Key=key)
        )
        return {
            "size": result["ContentLength"],
            "content_type": result.get("ContentType", ""),
            "etag": result.get("ETag", "").strip('"'),
            "metadata": result.get("Metadata", {}),
        }

    def copy(self, source_key, target_key, *, source_bucket=None, metadata=None):
        source = {"Bucket": source_bucket or self.bucket, "Key": source_key}
        self._request(
            lambda: self.client.copy_object(
                Bucket=self.bucket,
                Key=target_key,
                CopySource=source,
                MetadataDirective="COPY",
            )
        )
        info = self.get_metadata(target_key)
        return UploadResult(size=info["size"], checksum=self.checksum(target_key))

    def generate_signed_upload_url(
        self, key, *, content_type, expires_in, content_length=None
    ):
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
        }
        if content_length is not None:
            params["ContentLength"] = content_length
        return self._request(
            lambda: self.client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        )

    def generate_signed_download_url(self, key, *, expires_in, filename=None):
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self._request(
            lambda: self.client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        )

    def check_bucket(self):
        self._request(lambda: self.client.head_bucket(Bucket=self.bucket))
        return True
