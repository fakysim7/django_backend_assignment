import hashlib
import mimetypes
import re
import unicodedata
from pathlib import PurePath

from django.conf import settings
from rest_framework.exceptions import ValidationError

ALLOWED_EXTENSIONS = {
    ".pdf": {"application/pdf"},
    ".txt": {"text/plain"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".gif": {"image/gif"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
}


def normalize_filename(filename):
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized).strip()
    normalized = re.sub(r"[^\w .()\-]", "_", normalized, flags=re.UNICODE)
    normalized = normalized.strip(" .")
    if not normalized or normalized in {".", ".."}:
        raise ValidationError({"file": "Недопустимое имя файла."})
    return normalized[:255]


def expected_content_type(filename):
    extension = PurePath(filename).suffix.lower()
    content_types = ALLOWED_EXTENSIONS.get(extension)
    if not content_types:
        raise ValidationError({"file": "Расширение файла не поддерживается."})
    return extension, content_types


def validate_upload_descriptor(filename, content_type, size):
    normalized = normalize_filename(filename)
    extension, allowed_types = expected_content_type(normalized)
    if content_type not in allowed_types:
        guessed = mimetypes.guess_type(normalized)[0]
        if content_type != guessed or guessed not in allowed_types:
            raise ValidationError({"file": "Тип файла не соответствует расширению."})
    if not isinstance(size, int) or size <= 0 or size > settings.STORAGE_MAX_FILE_SIZE:
        raise ValidationError({"file": "Недопустимый размер файла."})
    return normalized, extension


def validate_signature(prefix, filename, content_type):
    extension = PurePath(filename).suffix.lower()
    signatures = {
        ".pdf": prefix.startswith(b"%PDF-"),
        ".png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": prefix.startswith(b"\xff\xd8\xff"),
        ".jpeg": prefix.startswith(b"\xff\xd8\xff"),
        ".webp": len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP",
        ".gif": prefix.startswith((b"GIF87a", b"GIF89a")),
        ".docx": prefix.startswith(b"PK\x03\x04"),
        ".txt": b"\x00" not in prefix,
    }
    if not signatures.get(extension, False):
        raise ValidationError({"file": "Содержимое файла не соответствует его типу."})
    if extension == ".txt":
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError({"file": "Текстовый файл должен быть в UTF-8."}) from error
    if extension in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        expected = ALLOWED_EXTENSIONS[extension]
        if content_type not in expected:
            raise ValidationError({"file": "Тип изображения не соответствует содержимому."})


def checksum_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
