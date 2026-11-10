"""Small OSS boundary. Binary data never passes through the web process."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class UploadedPart:
    number: int
    size: int
    etag: str


class OssMediaStore:
    def __init__(self, settings: Settings) -> None:
        if not settings.media_oss_enabled:
            raise RuntimeError("OSS 直传尚未启用")
        import oss2

        self._oss2 = oss2
        auth = oss2.ProviderAuthV4(oss2.credentials.EnvironmentVariableCredentialsProvider())
        # The website may still run outside Hangzhou during grey rollout.
        # Control calls are tiny and use the reachable public endpoint; only
        # Function Compute should use the same-region internal endpoint.
        self._control = oss2.Bucket(
            auth,
            settings.media_oss_public_endpoint,
            settings.media_oss_bucket,
            region=settings.media_oss_region,
        )
        self._public = oss2.Bucket(
            auth,
            settings.media_oss_public_endpoint,
            settings.media_oss_bucket,
            region=settings.media_oss_region,
        )

    def initiate(self, key: str) -> str:
        return self._control.init_multipart_upload(key).upload_id

    def abort(self, key: str, upload_id: str) -> None:
        self._control.abort_multipart_upload(key, upload_id)

    def sign_part(self, key: str, upload_id: str, number: int) -> str:
        return self._public.sign_url(
            "PUT", key, 600,
            params={"uploadId": upload_id, "partNumber": str(number)},
            slash_safe=True,
        )

    def list_parts(self, key: str, upload_id: str) -> list[UploadedPart]:
        parts: list[UploadedPart] = []
        marker: str | int = ""
        while True:
            page = self._control.list_parts(key, upload_id, marker=marker)
            parts.extend(
                UploadedPart(int(part.part_number), int(part.size), str(part.etag))
                for part in page.parts
            )
            if not page.is_truncated:
                return parts
            marker = page.next_part_number_marker

    def complete(self, key: str, upload_id: str, parts: list[UploadedPart]) -> None:
        manifest = [
            self._oss2.models.PartInfo(part.number, part.etag)
            for part in parts
        ]
        self._control.complete_multipart_upload(key, upload_id, manifest)

    def object_size(self, key: str) -> int:
        return int(self._control.head_object(key).content_length)

    def sign_download(self, key: str) -> str:
        return self._public.sign_url("GET", key, 300, slash_safe=True)
