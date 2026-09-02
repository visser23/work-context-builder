"""File convertibility checking — determines if a file can be meaningfully
converted to Markdown BEFORE downloading or processing it.

Used by SharePoint web source to skip unconvertible files from metadata,
and by local folder/SharePoint local sources to skip binary files early.
"""

from __future__ import annotations

from pathlib import Path

from workctx.normalise.office import SUPPORTED_EXTENSIONS as OFFICE_EXTENSIONS

_PDF_EXTENSIONS = {".pdf"}

_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".text", ".log", ".cfg", ".ini",
    ".yaml", ".yml", ".toml", ".conf", ".config", ".properties",
    ".env", ".editorconfig", ".gitignore", ".dockerignore",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".cs", ".fs",
    ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm",
    ".r", ".R", ".jl", ".lua", ".pl", ".pm", ".ex", ".exs",
    ".sql", ".ddl", ".dml",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".psm1",
    ".css", ".scss", ".less", ".sass",
    ".tf", ".hcl", ".json5", ".graphql", ".gql", ".proto",
    ".makefile", ".mk", ".cmake",
    ".rst", ".adoc", ".asciidoc", ".tex", ".bib", ".wiki",
    ".dockerfile", ".containerfile",
    ".vue", ".svelte", ".astro",
    ".url", ".webloc",
}

ALL_CONVERTIBLE_EXTENSIONS = OFFICE_EXTENSIONS | _PDF_EXTENSIONS | _TEXT_EXTENSIONS

_NEVER_DOWNLOAD_EXTENSIONS = {
    ".mov", ".mp4", ".mp3", ".wav", ".avi", ".wmv", ".mkv",
    ".m4a", ".m4v", ".webm", ".flac", ".aac", ".ogg", ".wma",
    ".flv", ".3gp", ".3g2",
    ".iso", ".dmg", ".msi", ".exe", ".dll", ".so", ".dylib",
    ".bin", ".dat", ".bak",
    ".fig", ".sketch", ".xd", ".ai", ".psd", ".indd",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif",
    ".ico", ".svg", ".webp", ".heic", ".heif", ".raw", ".cr2",
    ".nef", ".arw", ".dng",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".one", ".onetoc2",
    ".eps", ".vsdx", ".vsd",
    ".apk", ".ipa",
}

MAX_DOWNLOAD_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB


def can_convert(filename: str) -> bool:
    """Check if a file can be converted to Markdown based on extension."""
    suffix = Path(filename).suffix.lower()
    if suffix in _NEVER_DOWNLOAD_EXTENSIONS:
        return False
    if suffix in ALL_CONVERTIBLE_EXTENSIONS:
        return True
    return bool(suffix)


def should_skip_download(filename: str, file_size: int | None = None) -> tuple[bool, str]:
    """Decide whether to skip downloading a file.

    Returns (should_skip, reason) tuple.
    """
    suffix = Path(filename).suffix.lower()

    if suffix in _NEVER_DOWNLOAD_EXTENSIONS:
        return True, f"unconvertible format ({suffix})"

    if file_size and file_size > MAX_DOWNLOAD_SIZE_BYTES:
        size_mb = file_size / 1024 / 1024
        limit_mb = MAX_DOWNLOAD_SIZE_BYTES / 1024 / 1024
        return True, f"too large ({size_mb:.0f} MB > {limit_mb:.0f} MB limit)"

    return False, ""
