import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dompurify_script_loaded():
    html = (ROOT / ".ai/dashboard/index.html").read_text(encoding="utf-8")

    # Match the dompurify script tag tolerantly: it may carry extra
    # supply-chain hardening attributes (crossorigin, integrity, data-*)
    # and may wrap across lines.
    assert re.search(
        r'<script\b[^>]*\bsrc="https://cdn\.jsdelivr\.net/npm/dompurify@3\.\d+\.\d+/dist/purify\.min\.js"[^>]*>\s*</script>',
        html,
        flags=re.DOTALL,
    )


def test_all_marked_parse_calls_are_sanitized():
    files = [
        ROOT / ".ai/dashboard/app/skills.js",
        ROOT / ".ai/dashboard/app/agents.js",
        ROOT / ".ai/dashboard/app/core.js",
        ROOT / ".ai/dashboard/app/terminals.js",
    ]

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"innerHTML\s*=\s*marked\.parse\(", text), path

        for line_no, line in enumerate(text.splitlines(), start=1):
            if "marked.parse(" in line:
                assert "DOMPurify.sanitize(marked.parse(" in line, (
                    f"{path.relative_to(ROOT)}:{line_no} has unsanitized marked.parse"
                )


def test_terminals_image_chip_no_innerHTML_interpolation():
    text = (ROOT / ".ai/dashboard/app/terminals.js").read_text(encoding="utf-8")

    assert not re.search(r"chip\.innerHTML\s*=\s*`<img\s+src=\"\$\{", text)
    assert "image\\/(png|jpeg|gif|webp)" in text
    attach_render = text[text.index("function termRenderAttachments"):]
    attach_render = attach_render[: attach_render.index("var _IMAGE_PASTE_MAX_BYTES")]
    assert 'document.createElement("img")' in attach_render
