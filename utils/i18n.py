import locale
import logging
import os
from typing import Optional

from PySide6.QtCore import QSettings, QTranslator
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

SUPPORTED_UI_LANGUAGES = ("zh_en", "zh", "en")


def normalize_ui_language(value: Optional[str], default: str = "zh_en") -> str:
    text = str(value or "").strip().lower()
    if text in SUPPORTED_UI_LANGUAGES:
        return text
    return default


def get_system_language() -> str:
    lang = locale.getlocale()[0] or locale.getdefaultlocale()[0]
    if not lang:
        return "en"
    lowered = str(lang).lower()
    if lowered.startswith("zh") or "_cn" in lowered:
        return "zh"
    return "en"


def choose_ui_language(user_pref: Optional[str]) -> str:
    normalized = normalize_ui_language(user_pref, default="")
    if normalized:
        return normalized
    sys_lang = get_system_language()
    if sys_lang == "zh":
        return "zh_en"
    return "en"


def bi_text(zh: str, en: str, ui_language: str = "zh_en") -> str:
    lang = normalize_ui_language(ui_language)
    if lang == "zh":
        return zh
    if lang == "en":
        return en
    return f"{zh} ({en})"


def install_translator(
    app: QApplication,
    *,
    base_dir: Optional[str] = None,
    language: Optional[str] = None,
) -> Optional[QTranslator]:
    ui_lang = normalize_ui_language(language, default="")
    if not ui_lang:
        settings = QSettings("MalodyAnalytics", "MalodyAnalyticsTool")
        ui_lang = choose_ui_language(settings.value("ui_language", ""))
    # Current UI mainly uses inline bilingual strings. Translator is optional and
    # only used for non-bilingual mode.
    if ui_lang not in {"zh", "en"}:
        return None
    if ui_lang == "en":
        return None

    translator = QTranslator(app)
    rel = os.path.join("translations", "malody_zh_CN.qm")
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, rel))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", rel))

    for candidate in candidates:
        abs_path = os.path.abspath(candidate)
        if not os.path.exists(abs_path):
            continue
        if translator.load(abs_path):
            app.installTranslator(translator)
            logger.info("Loaded translation file: %s", abs_path)
            return translator
    logger.warning("Chinese translation file not loaded; continuing with inline labels.")
    return None


def set_global_font(app: QApplication) -> None:
    font = QFont(app.font())
    fallback_fonts = [
        "Microsoft YaHei",
        "Microsoft JhengHei",
        "SimHei",
        "NSimSun",
        "PingFang SC",
        "Hiragino Sans GB",
        "STHeiti",
        "Noto Sans CJK SC",
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "Source Han Sans SC",
    ]
    for family in fallback_fonts:
        font.setFamily(family)
        app.setFont(font)
        if app.font().family() == family:
            logger.info("Using font family: %s", family)
            return
    logger.info("Using default font family")
