"""Minimal markdown -> QTextDocument-safe HTML for chat surfaces.

QTextEdit / QTextBrowser render Qt's HTML subset natively but show markdown
markers (``**bold**``) literally. This module converts the small markdown
vocabulary the assistant actually emits — bold, bullets, ``#`` headings,
inline code — into escaped HTML, so plain chat text stays visually identical
(everything is escaped first) while formatted text stops leaking markers.

The output is always wrapped in a ``<span>`` so Qt reliably takes the rich
text path; ``&lt;``-style escapes would otherwise render literally.
"""

from __future__ import annotations

import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]*)`")
_LIST_RE = re.compile(r"^(\s*)[*\-•·]\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")

# HTML <sub>/<sup> → Unicode（论文常见上下标：AI 输出 E<sub>0</sub>=mc<sup>2</sup>
# 这类形态时，escape 会把标签变成字面文本；转为 Unicode 后正常显示且保持防注入）。
_SUB_HTML_RE = re.compile(r"<sub>(.*?)</sub>", re.S | re.I)
_SUP_HTML_RE = re.compile(r"<sup>(.*?)</sup>", re.S | re.I)

_SUBSCRIPT_MAP = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌",
    "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ",
    "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
})
_SUPERSCRIPT_MAP = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼",
    "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
})

# ---------------------------------------------------------------------------
# LaTeX → Unicode（M4.10 体验修复：AI 输出 $...$ / $$...$$ 公式时转为可读文本）
# ---------------------------------------------------------------------------

_LATEX_GREEK: dict[str, str] = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

_LATEX_OPERATORS: dict[str, str] = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡",
    "infty": "∞", "partial": "∂", "nabla": "∇", "propto": "∝",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃",
    "cup": "∪", "cap": "∩", "emptyset": "∅",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "perp": "⊥", "parallel": "∥",
    "angle": "∠", "degree": "°",
}

_LATEX_FUNCS: dict[str, str] = {
    "sin": "sin", "cos": "cos", "tan": "tan", "log": "log",
    "ln": "ln", "exp": "exp", "min": "min", "max": "max",
}

_LATEX_GREEK_RE = re.compile(
    r"\\(" + "|".join(_LATEX_GREEK) + r")\b"
)
_LATEX_OP_RE = re.compile(
    r"\\(" + "|".join(_LATEX_OPERATORS) + r")\b"
)
_LATEX_FUNC_RE = re.compile(
    r"\\(" + "|".join(_LATEX_FUNCS) + r")\b"
)
_LATEX_MATHBF_RE = re.compile(r"\\mathbf\{([^}]*)\}")
_LATEX_MATHIT_RE = re.compile(r"\\mathit\{([^}]*)\}")
_LATEX_FRAC_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_LATEX_TEXT_RE = re.compile(r"\\text\{([^}]*)\}")
_LATEX_DOLLAR_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
_LATEX_DOLLAR_INLINE_RE = re.compile(r"\$([^$\n]+?)\$")


def _latex_to_unicode(text: str) -> str:
    """将常见 LaTeX 命令转为 Unicode 等价物（零依赖, 纯文本替换）。

    处理顺序: mathbf/mathit → frac → greek → operators → funcs → text。
    不处理复杂嵌套（矩阵/积分等）——那些保持原样但去掉 $ 定界符。
    """
    # \frac{a}{b} → a/b
    text = _LATEX_FRAC_RE.sub(r"\1/\2", text)
    # \mathbf{X} → X（Unicode 粗体映射太复杂, 直接去壳）
    text = _LATEX_MATHBF_RE.sub(r"\1", text)
    # \mathit{X} → X
    text = _LATEX_MATHIT_RE.sub(r"\1", text)
    # \text{X} → X
    text = _LATEX_TEXT_RE.sub(r"\1", text)
    # 希腊字母
    text = _LATEX_GREEK_RE.sub(lambda m: _LATEX_GREEK[m.group(1)], text)
    # 运算符
    text = _LATEX_OP_RE.sub(lambda m: _LATEX_OPERATORS[m.group(1)], text)
    # 函数名
    text = _LATEX_FUNC_RE.sub(lambda m: _LATEX_FUNCS[m.group(1)], text)
    return text


def _latex_blocks_to_unicode(text: str) -> str:
    """处理 $$...$$ display 块和 $...$ inline 公式。

    只做命令→Unicode 替换 + 去 $ 定界符; 不尝试完整排版。
    """
    # display 块
    def _display_sub(m):
        inner = _latex_to_unicode(m.group(1).strip())
        return f" [{inner}] " if inner else ""
    text = re.sub(r"\$\$(.+?)\$\$", _display_sub, text, flags=re.S)

    # inline 公式
    def _inline_sub(m):
        inner = _latex_to_unicode(m.group(1).strip())
        return inner if inner else m.group(0)
    text = re.sub(r"\$([^$\n]+?)\$", _inline_sub, text)
    return text

# QTextDocument 会把任何输入转换为它自己的标准标签子集；toHtml() 输出若
# 出现该白名单之外的标签，说明上游注入了未转义的 HTML（验收测试用）。
_KNOWN_QT_TAGS = frozenset({
    "html", "head", "meta", "style", "title", "body", "p", "span", "br",
    "div", "b", "i", "u", "s", "code", "pre", "em", "strong", "sub", "sup",
    "table", "tbody", "thead", "tr", "td", "th", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "a", "font", "img",
    "blockquote", "center", "nobr",
})


def _inline(escaped: str) -> str:
    """Format inline markers on already-escaped text."""
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped


def _sub_sup_to_unicode(text: str) -> str:
    """Convert literal HTML <sub>/<sup> into Unicode sub/superscripts.

    Runs before html.escape so AI-emitted `E<sub>0</sub>=mc<sup>2</sup>`
    renders as E₀=mc² instead of leaking raw tags as literal text. Unmapped
    characters keep their original form (safe no-op per character).
    """
    text = _SUB_HTML_RE.sub(lambda m: m.group(1).translate(_SUBSCRIPT_MAP), text)
    text = _SUP_HTML_RE.sub(
        lambda m: m.group(1).translate(_SUPERSCRIPT_MAP), text)
    return text


def markdown_to_html(text: str) -> str:
    """Render assistant text as QTextDocument-friendly HTML.

    Plain text without markdown passes through escaped and visually
    unchanged; markdown lines are converted line by line.
    """
    if not text:
        return ""
    text = _sub_sup_to_unicode(text)
    text = _latex_blocks_to_unicode(text)
    rendered: list[str] = []
    for raw in text.splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            rendered.append(f"<b>{html.escape(m.group(1))}</b>")
            continue
        m = _LIST_RE.match(raw)
        if m:
            indent = "&nbsp;" * len(m.group(1).replace("\t", "    "))
            rendered.append(f"{indent}•&nbsp;{_inline(html.escape(m.group(2)))}")
            continue
        rendered.append(_inline(html.escape(raw)))
    body = "<br>".join(rendered)
    return f"<span>{body}</span>"


__all__ = ["markdown_to_html"]
