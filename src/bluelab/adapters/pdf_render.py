"""C-9 report-PDF renderer with an isolated headless Chromium instance.

The renderer accepts a deliberately small, already concealment-safe HTML document.
It never receives a database session, an object-store client, a URL, or a path from
the report itself.  Chromium consequently has no reason to fetch a network, local
file, or instance-metadata resource; a request interceptor and a restrictive CSP
make that property executable rather than conventional (SEC-016).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PdfRenderError(RuntimeError):
    """A report did not become a PDF and may be retried by its worker."""


class PdfRenderer(Protocol):
    """The narrow PDF capability consumed by the report worker."""

    async def render(self, html_document: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class EmbeddedFonts:
    """The three typefaces required by ADR-0021, encoded into the document."""

    arabic_body: Path
    arabic_heading: Path
    latin: Path

    def css(self) -> str:
        """Return data-URI ``@font-face`` declarations; fail closed when absent."""
        return "\n".join(
            (
                self._face("BlueLabNaskh", self.arabic_body),
                self._face("BlueLabKufi", self.arabic_heading),
                self._face("BlueLabLatin", self.latin),
            )
        )

    @staticmethod
    def _face(name: str, path: Path) -> str:
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise PdfRenderError("required embedded PDF font is unavailable") from exc
        return (
            f"@font-face{{font-family:{name};src:url(data:font/ttf;base64,{encoded})"
            " format('truetype');font-display:block;}"
        )


class PlaywrightPdfRenderer:
    """Print a static document after the browser reports every font ready."""

    def __init__(self, fonts: EmbeddedFonts) -> None:
        self._fonts = fonts

    async def render(self, html_document: str) -> bytes:
        """Render one static report without allowing Chromium any egress.

        The page uses an about:blank document.  Only inline CSS and ``data:`` font
        values are permitted; every attempted fetch, including ``file:`` and the
        instance-metadata address, is aborted before it can leave Chromium.
        """
        try:
            from playwright.async_api import async_playwright

            html_with_fonts = html_document.replace("/*__BLUELAB_EMBEDDED_FONTS__*/", self._fonts.css())
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=("--disable-gpu", "--disable-dev-shm-usage"),
                )
                try:
                    context = await browser.new_context(
                        java_script_enabled=True,
                        service_workers="block",
                    )
                    page = await context.new_page()

                    async def block_request(route: object) -> None:
                        await route.abort()  # type: ignore[attr-defined]

                    await page.route("**/*", block_request)
                    await page.set_content(html_with_fonts, wait_until="domcontentloaded")
                    fonts_ready = await page.evaluate(
                        """async () => { await document.fonts.ready; return document.fonts.status === 'loaded'; }"""
                    )
                    if fonts_ready is not True:
                        raise PdfRenderError("PDF fonts did not become ready")
                    return await page.pdf(
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={"top": "16mm", "right": "16mm", "bottom": "16mm", "left": "16mm"},
                    )
                finally:
                    await browser.close()
        except PdfRenderError:
            raise
        except Exception as exc:
            raise PdfRenderError("Chromium report render failed") from exc


def default_embedded_fonts() -> EmbeddedFonts:
    """Resolve only fonts packaged in the worker image, never user input."""
    return EmbeddedFonts(
        arabic_body=Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        arabic_heading=Path("/usr/share/fonts/truetype/noto/NotoKufiArabic-Regular.ttf"),
        latin=Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    )
