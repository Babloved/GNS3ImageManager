#!/usr/bin/env python3
"""
GNS3 Image Manager — управление образами QEMU с GUI и интеграцией с API GNS3.
Возможности:
- просмотр, добавление, удаление образов
- большой диалог выбора файлов (с фильтрацией по образам)
- создание шаблонов QEMU (расширенные настройки: символ, категория, интерфейсы дисков)
- настройка папки по умолчанию для импорта образов
- linked clone включён по умолчанию
- автоматическое удаление связанных шаблонов и узлов при удалении образа
- удаление образов, не привязанных ни к одному шаблону
- удаление шаблонов с отсутствующими образами
- управление конфигурационными ISO (nocloud для NGFW, конфиги Cisco, MikroTik)
- создание ISO через mkisofs (гарантирует совместимость с GNS3)
- тёмная тема, сортировка столбцов, индикатор состояния API
- иконка приложения (если упакован PyInstaller)
- все окна открываются на весь экран
- удобное копирование текста ошибок
- редакторы конфигов работают как «Блокнот»: Ctrl+Z/Y, Ctrl+A, Ctrl+X/C/V, Ctrl+F/H, Ctrl+S, масштаб и контекстное меню
- загрузка настроек из существующего ISO при создании нового (исправлено)
- ПОДДЕРЖКА ZIP-АРХИВОВ: автоматическая распаковка образов из ZIP при добавлении

Требования: Python 3.6+, pip install requests send2trash pycdlib (send2trash опционально)
           Для создания ISO требуется установленный mkisofs (genisoimage) в PATH.
"""

import os
import sys
import json
import re
import shutil
import subprocess
import tempfile
import traceback
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
from pathlib import Path
from datetime import datetime
from io import BytesIO
import zipfile  # <--- ДОБАВЛЕНО для работы с ZIP

import requests
from requests.auth import HTTPBasicAuth

# -------------------- Конфигурация --------------------
TARGET_DIR = Path.home() / "GNS3/images/QEMU"
IMAGE_EXTENSIONS = {".qcow2", ".img", ".vmdk", ".qcow", ".raw", ".vdi", ".vhd", ".vhdx"}
CONFIG_FILE = Path.home() / ".gns3_image_manager_config.json"
CONFIG_ISO_DIR = TARGET_DIR / "config_isos"            # папка для конфигурационных ISO
LOG_FILE = Path.home() / ".gns3_image_manager.log"      # журнал действий (для диагностики)
DEFAULT_IMPORT_DIR = str(Path.home() / "Downloads") if (Path.home() / "Downloads").exists() else str(Path.home())


def log_event(message):
    """Дописывает строку в журнал приложения (не мешает работе при ошибке)."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except Exception:
        pass

DEFAULT_CONFIG = {
    "gns3_host": "http://localhost:3080",
    "gns3_user": "",
    "gns3_pass": "",
    "qemu_path": "/usr/bin/qemu-system-x86_64",
    "default_ram": 256,
    "default_cpus": 1,
    "adapter_type": "e1000",
    "adapters": 1,
    "boot_priority": "c",
    "console_type": "telnet",
    "platform": "x86_64",
    "options": "",
    "default_symbol": ":/symbols/classic/qemu_guest.svg",
    "default_category": "router",
    "import_dir": DEFAULT_IMPORT_DIR,
    "linked_clone": True,
    "hda_disk_interface": "sata",
    "hdb_disk_interface": "sata"
}

# -------------------- Загрузка/сохранение конфига --------------------
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# -------------------- Проверка наличия mkisofs --------------------
def find_mkisofs():
    """Проверяет наличие mkisofs или genisoimage в PATH."""
    for name in ["mkisofs", "genisoimage"]:
        if shutil.which(name):
            return name
    return None

MKISOFS_CMD = find_mkisofs()
if MKISOFS_CMD is None:
    print("WARNING: mkisofs/genisoimage не найден. Создание ISO будет недоступно.", file=sys.stderr)

# -------------------- Создание ISO через mkisofs --------------------
def create_iso_with_mkisofs(output_path, files_dict, vol_id="cidata"):
    """
    Создаёт ISO с помощью mkisofs.
    files_dict: {имя_файла: содержимое_в_виде_строки}
    """
    if MKISOFS_CMD is None:
        raise RuntimeError("Утилита mkisofs/genisoimage не установлена. Установите её (например, apt install genisoimage).")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        for fname, content in files_dict.items():
            file_path = tmp_path / fname
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        cmd = [
            MKISOFS_CMD,
            "-joliet",
            "-rock",
            "-volid", vol_id,
            "-output", str(output_path),
            str(tmp_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"mkisofs ошибка: {result.stderr}")

def create_nocloud_iso(output_path, user_data, meta_data=None, vendor_data=None):
    """Создаёт nocloud ISO с помощью mkisofs."""
    files = {
        'user-data': user_data,
        'meta-data': meta_data or "instance-id: i-ngfw\nlocal-hostname: ngfw\n"
    }
    if vendor_data:
        files['vendor-data'] = vendor_data
    create_iso_with_mkisofs(output_path, files, vol_id="cidata")

def create_generic_config_iso(output_path, file_name, content, vol_id="CONFIG"):
    """Создаёт ISO с одним файлом конфигурации."""
    base_name = os.path.basename(file_name)
    files = {base_name: content}
    create_iso_with_mkisofs(output_path, files, vol_id=vol_id)

# -------------------- Чтение содержимого ISO (исправлено) --------------------
def read_iso_content(iso_path):
    """
    Извлекает содержимое всех файлов из корня ISO с помощью isoinfo.
    Возвращает словарь {имя_файла: содержимое_в_виде_строки}
    """
    import subprocess
    import tempfile

    # Проверяем наличие isoinfo
    if shutil.which("isoinfo") is None:
        # Если isoinfo нет, пробуем использовать pycdlib как запасной вариант
        try:
            return _read_iso_with_pycdlib(iso_path)
        except:
            raise RuntimeError("Утилита isoinfo не найдена. Установите genisoimage (содержит isoinfo) или установите pycdlib.")

    # Получаем список всех файлов в ISO (рекурсивно) с путями
    try:
        result = subprocess.run(
            ["isoinfo", "-R", "-J", "-f", "-i", str(iso_path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore"
        )
    except Exception as e:
        raise RuntimeError(f"Не удалось выполнить isoinfo: {e}")

    if result.returncode != 0:
        raise RuntimeError(f"Ошибка isoinfo: {result.stderr}")

    # Парсим вывод: каждая строка — путь к файлу (начинается с '/')
    file_paths = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('/'):
            # Убираем возможные версии (;1) и завершающий слеш для каталогов
            # Нас интересуют только файлы (не каталоги)
            # isoinfo -f возвращает пути даже для каталогов, но мы отфильтруем их позже
            if not line.endswith('/'):  # простое правило: каталоги заканчиваются на '/'
                # Удаляем версию ;1 если есть
                if ';' in line:
                    line = line[:line.index(';')]
                file_paths.append(line)

    if not file_paths:
        # Если файлов не найдено, пробуем pycdlib
        try:
            return _read_iso_with_pycdlib(iso_path)
        except:
            raise RuntimeError("В ISO не найдено файлов с данными (isoinfo не нашёл файлов).")

    content_dict = {}
    # Для каждого файла извлекаем содержимое
    for fpath in file_paths:
        # Извлекаем содержимое файла через isoinfo -x
        try:
            proc = subprocess.run(
                ["isoinfo", "-R", "-J", "-x", fpath, "-i", str(iso_path)],
                capture_output=True, text=False  # получаем байты
            )
        except Exception as e:
            continue  # если не удалось извлечь, пропускаем

        if proc.returncode != 0:
            continue

        # Декодируем содержимое, игнорируя ошибки
        data = proc.stdout.decode('utf-8', errors='ignore')
        # Имя файла — последний компонент пути
        name = os.path.basename(fpath)
        # Если имя уже есть, добавляем суффикс (на случай дубликатов)
        if name in content_dict:
            base, ext = os.path.splitext(name)
            content_dict[f"{base}_1{ext}"] = data
        else:
            content_dict[name] = data

    if not content_dict:
        raise RuntimeError("В ISO не найдено файлов с данными (isoinfo не извлёк ни одного файла).")
    return content_dict

def _read_iso_with_pycdlib(iso_path):
    """Запасной вариант чтения ISO через pycdlib (оригинальный код)."""
    try:
        import pycdlib
    except ImportError:
        raise RuntimeError("Установите pycdlib: pip install pycdlib")

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    result = {}

    def walk_dir(path):
        try:
            entries = iso.listdir(path)
        except Exception:
            return
        for entry in entries:
            if entry in ('.', '..'):
                continue
            full_path = path.rstrip('/') + '/' + entry if path != '/' else '/' + entry
            try:
                fp = iso.open_file_from_iso(full_path)
                data = fp.read().decode('utf-8', errors='ignore')
                fp.close()
                clean_name = entry.split(';')[0] if ';' in entry else entry
                result[clean_name] = data
            except Exception:
                walk_dir(full_path)

    walk_dir('/')
    iso.close()
    return result

# ==================== ДОБАВЛЕНО: Распаковка ZIP ====================
def extract_images_from_zip(zip_path, dest_dir):
    """
    Извлекает из ZIP-архива все файлы с расширениями из IMAGE_EXTENSIONS
    в папку dest_dir. Возвращает список путей к извлечённым файлам.
    При конфликте имён добавляет суффикс (1), (2) и т.д.
    """
    extracted = []
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("Файл не является ZIP-архивом")

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Получаем список всех файлов в архиве (включая пути)
        for info in zf.infolist():
            # Пропускаем каталоги
            if info.is_dir():
                continue
            # Проверяем расширение
            ext = os.path.splitext(info.filename)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                continue

            # Извлекаем имя файла без пути
            fname = os.path.basename(info.filename)
            # Проверяем конфликт имён
            target_path = dest_dir / fname
            counter = 1
            while target_path.exists():
                base, ext2 = os.path.splitext(fname)
                new_name = f"{base}_{counter}{ext2}"
                target_path = dest_dir / new_name
                counter += 1

            # Извлекаем файл в dest_dir с новым именем
            with zf.open(info) as src, open(target_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target_path)

    return extracted

# -------------------- Тема в стиле VS Code Dark Modern --------------------
THEME = {
    "background": "#1f1f1f",      # editor.background
    "surface": "#292929",         # мягко отделённые поля и панели
    "surface_hover": "#323232",
    "text": "#b8b8b8",            # менее контрастный светло-серый
    "muted": "#858585",           # вторичный текст
    "accent": "#0078d4",          # button/focusBorder
    "accent_hover": "#026ec1",    # button.hoverBackground
    "danger": "#303030",          # нейтральное оформление опасных действий
    "danger_hover": "#414141",
}
def set_dark_theme(root):
    style = ttk.Style()
    root.tk_setPalette(background=THEME["background"], foreground=THEME["text"],
                       activeBackground=THEME["surface_hover"], activeForeground=THEME["text"])
    style.theme_use('clam')
    style.configure(".", background=THEME["background"], foreground=THEME["text"],
                    fieldbackground=THEME["surface"], troughcolor=THEME["background"],
                    selectbackground=THEME["accent"], selectforeground=THEME["text"],
                    font=("Segoe UI", 10))
    style.configure("Treeview", background=THEME["background"], foreground=THEME["text"],
                    fieldbackground=THEME["background"], rowheight=29, borderwidth=0)
    style.configure("Treeview.Heading", background=THEME["surface"], foreground=THEME["muted"],
                    relief="flat", padding=(10, 7), font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[('selected', THEME["accent"])])
    style.configure("TLabel", background=THEME["background"], foreground=THEME["text"])
    style.configure("TButton", background=THEME["surface"], foreground=THEME["text"],
                    borderwidth=1, focusthickness=2, padding=(10, 6))
    style.map("TButton", background=[('active', THEME["surface_hover"])])
    # Иерархия действий строится на поверхности и рамке, а не на ярких цветах.
    style.configure("Accent.TButton", background="#3a3a3a", foreground="#d0d0d0",
                    borderwidth=1, padding=(11, 6))
    style.map("Accent.TButton", background=[('active', "#484848")])
    style.configure("Danger.TButton", background=THEME["danger"], foreground="#aaaaaa",
                    borderwidth=1)
    style.map("Danger.TButton", background=[('active', THEME["danger_hover"])])
    style.configure("TEntry", foreground=THEME["text"], fieldbackground=THEME["surface"], padding=6)
    style.configure("TCombobox", foreground=THEME["text"], fieldbackground=THEME["surface"],
                    background=THEME["surface"], arrowcolor=THEME["text"], padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", THEME["surface"])],
              foreground=[("readonly", THEME["text"])],
              selectbackground=[("readonly", THEME["accent"])],
              selectforeground=[("readonly", THEME["text"])])
    # В Linux всплывающий список Combobox — отдельный Listbox, поэтому
    # его цвета нужно задавать через базу опций Tk, а не только через ttk.Style.
    for pattern, color in (
        ("*TCombobox*Listbox.background", THEME["surface"]),
        ("*TCombobox*Listbox.foreground", THEME["text"]),
        ("*TCombobox*Listbox.selectBackground", THEME["accent"]),
        ("*TCombobox*Listbox.selectForeground", THEME["text"]),
        ("*TCombobox*Listbox*Background", THEME["surface"]),
        ("*TCombobox*Listbox*Foreground", THEME["text"]),
    ):
        root.option_add(pattern, color)
    style.configure("TFrame", background=THEME["background"])
    style.configure("TScrollbar", background=THEME["surface"], troughcolor=THEME["background"], arrowsize=14)
    style.configure("TLabelframe", background=THEME["background"], foreground=THEME["muted"], padding=4)
    style.configure("TLabelframe.Label", background=THEME["background"], foreground=THEME["muted"], font=("Segoe UI", 9, "bold"))
    style.configure("TNotebook", background=THEME["background"], borderwidth=0)
    style.configure("TNotebook.Tab", background=THEME["surface"], foreground=THEME["muted"], padding=(14, 7))
    style.map("TNotebook.Tab", background=[('selected', THEME["accent"])], foreground=[('selected', THEME["text"])])
    root.option_add("*Text.Background", THEME["surface"])
    root.option_add("*Text.Foreground", THEME["text"])
    root.option_add("*Text.InsertBackground", THEME["text"])

# -------------------- Умное имя шаблона --------------------
def generate_template_name(image_path: str) -> str:
    """Формирует короткое, читаемое имя шаблона из имени файла образа."""
    name = Path(image_path).stem

    # Типичные имена: ngfw-x86-64-7.3.3.340281R-release-public.qcow2,
    # waf-x86-64-7.6.0.491986R-release-public.qcow2.
    # Хвосты вроде release-public намеренно не включаем в имя шаблона.
    match = re.match(
        r"^(ngfw|waf|mc|logan|swg|dcfw|siem)[_-]x86[-_]64[_-](\d+\.\d+\.\d+\.\d+[A-Za-z]?)",
        name,
        flags=re.IGNORECASE,
    )
    if match:
        product_names = {
            "ngfw": "NGFW", "waf": "WAF", "mc": "MC", "logan": "Logan",
            "swg": "SWG", "dcfw": "DCFW", "siem": "SIEM",
        }
        return f"{product_names[match.group(1).lower()]} {match.group(2)}"

    clean = re.sub(r'[-_]+', ' ', name)
    return ' '.join(word.capitalize() for word in clean.split())

# -------------------- Функция максимизации окна (кроссплатформенная) --------------------
def maximize_window(window):
    """Разворачивает окно на весь экран с сохранением рамок (не fullscreen).

    Запрос отправляется один раз: в Tk на X11 повторная установка атрибута
    `-zoomed` переключает состояние и окно возвращается к прежнему размеру.
    """
    try:
        window.state('zoomed')                     # Windows
        return
    except tk.TclError:
        pass
    try:
        window.attributes('-zoomed', True)         # Linux (X11)
        return
    except tk.TclError:
        pass
    # Запасной вариант — для оконных менеджеров без поддержки максимизации:
    # подгоняем окно под размер родителя, а не под весь рабочий стол, иначе
    # на нескольких мониторах окно растягивается на все сразу.
    try:
        window.update_idletasks()
        parent = window.master
        if isinstance(parent, (tk.Tk, tk.Toplevel)) and parent.winfo_width() > 1:
            window.geometry(f"{parent.winfo_width()}x{parent.winfo_height()}"
                            f"+{parent.winfo_rootx()}+{parent.winfo_rooty()}")
        else:
            window.geometry(f"{window.winfo_screenwidth()}x{window.winfo_screenheight()}+0+0")
    except tk.TclError:
        pass

# -------------------- Горячие клавиши и контекстное меню для текстовых полей --------------------
# Привязки вида <Control-z> сопоставляются по keysym, а keysym зависит от раскладки:
# на русской раскладке физическая клавиша Z отдаёт «Cyrillic_ya», и Ctrl+Z перестаёт
# работать (так же Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+X, Ctrl+S, Ctrl+F). Код клавиши
# (keycode) от раскладки не зависит, поэтому дополнительно перехватываем его.
if sys.platform.startswith("win"):
    _CTRL_KEYCODE_ACTIONS = {          # виртуальные коды клавиш Windows
        90: "z", 89: "y", 65: "a", 88: "x", 67: "c", 86: "v",
        70: "f", 72: "h", 83: "s", 48: "0", 187: "plus", 189: "minus",
    }
elif sys.platform == "darwin":
    _CTRL_KEYCODE_ACTIONS = {          # виртуальные коды клавиш macOS
        6: "z", 16: "y", 0: "a", 7: "x", 8: "c", 9: "v",
        3: "f", 4: "h", 1: "s", 29: "0", 24: "plus", 27: "minus",
    }
else:
    _CTRL_KEYCODE_ACTIONS = {          # X11/XKB keycodes (evdev, pc105)
        52: "z", 29: "y", 38: "a", 53: "x", 54: "c", 55: "v",
        41: "f", 43: "h", 39: "s", 19: "0", 21: "plus", 20: "minus",
    }


def install_text_editor_shortcuts(text_widget, editable=True, save_callback=None):
    """Настраивает tk.Text как привычный редактор («как в Блокноте»).

    Базовые операции (отмена/повтор, выделить всё, вырезать/копировать/вставить)
    выполняются штатными обработчиками Tk через виртуальные события <<Undo>>,
    <<Redo>>, <<SelectAll>>, <<Cut>>, <<Copy>>, <<Paste>>, поэтому поведение
    совпадает с обычными приложениями. Дополнительно добавляются:
      Ctrl+Y / Ctrl+Shift+Z    — повтор (в X11 у Tk это только Ctrl+Shift+Z);
      Ctrl+A                   — выделить всё (в X11 у Tk это Ctrl+/);
      Ctrl+Backspace / Ctrl+Del — удалить слово;
      Ctrl+F / Ctrl+H          — поиск и замена;
      Ctrl+S                   — сохранение (если задан save_callback);
      Ctrl+«+» / Ctrl+«-» / Ctrl+0 и Ctrl+колесо мыши — масштаб шрифта;
      правая кнопка мыши       — контекстное меню.

    Горячие клавиши работают при любой раскладке (в том числе русской).
    """
    try:
        # Заметное выделение: одинаково видно и когда поле в фокусе, и когда нет.
        text_widget.configure(undo=True, maxundo=-1, autoseparators=True,
                              selectbackground=THEME["accent"],
                              selectforeground="#ffffff",
                              inactiveselectbackground=THEME["accent"])
    except tk.TclError:
        pass

    # ----- масштаб шрифта -----
    try:
        original_font = tkfont.Font(font=text_widget.cget("font"))
    except tk.TclError:
        original_font = tkfont.Font(family="TkTextFont", size=10)
    original_size = original_font.cget("size")
    if original_size <= 0:
        original_size = 10
    editor_font = original_font.copy()
    text_widget.configure(font=editor_font)
    size_state = {"size": original_size}

    def _apply_size(size):
        size_state["size"] = max(6, min(72, size))
        editor_font.configure(size=size_state["size"])

    def _zoom_in(event=None):
        _apply_size(size_state["size"] + 1)
        return "break"

    def _zoom_out(event=None):
        _apply_size(size_state["size"] - 1)
        return "break"

    def _zoom_reset(event=None):
        _apply_size(original_size)
        return "break"

    def _zoom_wheel(event):
        delta = getattr(event, "delta", 0) or 0
        if getattr(event, "num", None) == 4 or delta > 0:
            return _zoom_in()
        if getattr(event, "num", None) == 5 or delta < 0:
            return _zoom_out()
        return "break"

    # ----- вспомогательные проверки -----
    def _is_editable():
        return text_widget.cget("state") != tk.DISABLED

    def _has_selection():
        try:
            text_widget.index(tk.SEL_FIRST)
            return True
        except tk.TclError:
            return False

    def _has_selection():
        try:
            text_widget.index(tk.SEL_FIRST)
            return True
        except tk.TclError:
            return False

    # ----- действия -----
    # Повторяем ровно то, что делают штатные обработчики Tk, но напрямую —
    # чтобы не зависеть от наличия его виртуальных событий.
    def _select_all(event=None):
        # копия обработчика <<SelectAll>> из Tk (каретка НЕ перемещается).
        # Важно: Ctrl+A в Tk на X11 — это <<LineStart>>, поэтому без этой
        # привязки каретка просто уезжает в начало строки.
        text_widget.tag_remove(tk.SEL, "1.0", tk.END)
        text_widget.tag_add(tk.SEL, "1.0", tk.END)
        return "break"

    def _undo(event=None):
        if not _is_editable():
            return "break"
        # копия обработчика <<Undo>> из Tk: без разделителей (edit separator)
        # стек отмены «слипается» с последующими правками и отмена ломается.
        try:
            text_widget.edit_separator()
            try:
                text_widget.edit_undo()
            except tk.TclError:
                pass
            text_widget.edit_separator()
        except tk.TclError:
            pass
        return "break"

    def _redo(event=None):
        if not _is_editable():
            return "break"
        try:
            text_widget.edit_redo()
        except tk.TclError:
            pass
        return "break"

    def _emit(virtual_event):
        def handler(event=None):
            try:
                text_widget.event_generate(virtual_event, when="now")
            except tk.TclError:
                pass
            return "break"
        return handler

    _cut = _emit("<<Cut>>")
    _paste = _emit("<<Paste>>")

    def _copy(event=None):
        """Копировать выделение, а в режиме только для чтения — весь текст."""
        if _has_selection() or _is_editable():
            return _emit("<<Copy>>")(event)
        data = text_widget.get("1.0", "end-1c")
        if not data:
            return "break"
        text_widget.clipboard_clear()
        text_widget.clipboard_append(data)
        return "break"

    def _delete_selection(event=None):
        if not _is_editable() or not _has_selection():
            return "break"
        text_widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        return "break"

    def _delete_word_before(event=None):
        if not _is_editable():
            return "break"
        try:
            start = text_widget.index("insert")
            while start != "1.0":                      # пропускаем пробелы слева
                prev = text_widget.index(f"{start}-1c")
                if text_widget.get(prev, start).strip():
                    break
                start = prev
            if start != "1.0":                          # захватываем слово целиком
                start = text_widget.index(f"{start}-1c wordstart")
            text_widget.delete(start, "insert")
        except tk.TclError:
            pass
        return "break"

    def _delete_word_after(event=None):
        if not _is_editable():
            return "break"
        try:
            last = text_widget.index("end-1c")
            end = text_widget.index("insert")
            while end != last:                          # пропускаем пробелы справа
                nxt = text_widget.index(f"{end}+1c")
                if text_widget.get(end, nxt).strip():
                    break
                end = nxt
            if end != last:                             # захватываем слово целиком
                end = text_widget.index(f"{end}+1c wordend")
            text_widget.delete("insert", end)
        except tk.TclError:
            pass
        return "break"

    def _save(event=None):
        if save_callback is not None:
            save_callback()
        return "break"

    def _open_find(event=None):
        dlg = getattr(text_widget, "_find_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.deiconify()
            dlg.lift()
            dlg.focus_set()
            return "break"
        text_widget._find_dialog = TextFindReplaceDialog(text_widget)
        return "break"

    # ----- перехват по keycode (раскладко-независимо) -----
    keycode_actions = {
        "a": _select_all, "c": _copy, "f": _open_find, "h": _open_find,
        "0": _zoom_reset, "plus": _zoom_in, "minus": _zoom_out,
    }
    if editable:
        keycode_actions.update({"z": _undo, "y": _redo, "x": _cut, "v": _paste})
        if save_callback is not None:
            keycode_actions["s"] = _save

    def _ctrl_keycode_dispatch(event):
        """Срабатывает, когда keysym не совпал (например, русская раскладка)."""
        name = _CTRL_KEYCODE_ACTIONS.get(event.keycode)
        if name is None:
            return None
        if name == "z" and (event.state & 0x1):    # Ctrl+Shift+Z — повтор
            name = "redo"
        handler = keycode_actions.get(name)
        return handler() if handler is not None else None

    # ----- контекстное меню -----
    menu = tk.Menu(text_widget, tearoff=0, bg=THEME["surface"], fg=THEME["text"],
                   activebackground=THEME["accent"], activeforeground=THEME["text"],
                   bd=1, relief=tk.SOLID)
    menu.add_command(label="Отменить", accelerator="Ctrl+Z", command=_undo)
    menu.add_command(label="Повторить", accelerator="Ctrl+Y", command=_redo)
    menu.add_separator()
    menu.add_command(label="Вырезать", accelerator="Ctrl+X", command=_cut)
    menu.add_command(label="Копировать", accelerator="Ctrl+C", command=_copy)
    menu.add_command(label="Вставить", accelerator="Ctrl+V", command=_paste)
    menu.add_command(label="Удалить", accelerator="Del", command=_delete_selection)
    menu.add_separator()
    menu.add_command(label="Выделить всё", accelerator="Ctrl+A", command=_select_all)
    menu.add_separator()
    menu.add_command(label="Найти/Заменить…", accelerator="Ctrl+F", command=_open_find)

    def _show_menu(event):
        has_sel = _has_selection()
        can_edit = _is_editable()
        has_text = bool(text_widget.get("1.0", "end-1c"))
        for label, enabled in (
            ("Отменить", can_edit),
            ("Повторить", can_edit),
            ("Вырезать", can_edit and has_sel),
            ("Копировать", has_sel or (not can_edit and has_text)),
            ("Вставить", can_edit),
            ("Удалить", can_edit and has_sel),
        ):
            menu.entryconfigure(label, state=tk.NORMAL if enabled else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    # ----- привязка клавиш -----
    key_bindings = [
        ("<Control-a>", _select_all),
        ("<Control-A>", _select_all),
        ("<Control-c>", _copy),
        ("<Control-C>", _copy),
        ("<Control-f>", _open_find),
        ("<Control-F>", _open_find),
        ("<Control-h>", _open_find),
        ("<Control-H>", _open_find),
        ("<Control-plus>", _zoom_in),
        ("<Control-equal>", _zoom_in),
        ("<Control-KP_Add>", _zoom_in),
        ("<Control-minus>", _zoom_out),
        ("<Control-KP_Subtract>", _zoom_out),
        ("<Control-Key-0>", _zoom_reset),
        ("<Control-MouseWheel>", _zoom_wheel),
        ("<Control-Button-4>", _zoom_wheel),
        ("<Control-Button-5>", _zoom_wheel),
    ]
    if editable:
        key_bindings += [
            ("<Control-z>", _undo),
            ("<Control-Z>", _redo),          # Ctrl+Shift+Z
            ("<Control-y>", _redo),
            ("<Control-Y>", _redo),
            ("<Control-Shift-Z>", _redo),
            ("<Control-x>", _cut),
            ("<Control-X>", _cut),
            ("<Control-v>", _paste),
            ("<Control-V>", _paste),
            ("<Control-BackSpace>", _delete_word_before),
            ("<Control-Delete>", _delete_word_after),
        ]
        if save_callback is not None:
            key_bindings += [("<Control-s>", _save), ("<Control-S>", _save)]

    for sequence, handler in key_bindings:
        try:
            text_widget.bind(sequence, handler)
        except tk.TclError:
            pass

    # Резервный перехват по keycode: нужен для нелатинских раскладок, где
    # keysym не совпадает с <Control-z> и подобными привязками.
    text_widget.bind("<Control-KeyPress>", _ctrl_keycode_dispatch)

    text_widget.bind("<Button-3>", _show_menu)
    text_widget.bind("<Button-2>", _show_menu)  # macOS / некоторые темы

    return text_widget


class TextFindReplaceDialog(tk.Toplevel):
    """Компактное окно «Поиск и замена» для текстового поля."""

    def __init__(self, text_widget):
        super().__init__(text_widget.winfo_toplevel())
        self.text = text_widget
        self.title("Поиск и замена")
        self.resizable(False, False)
        self.transient(text_widget.winfo_toplevel())

        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.match_case_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar()

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Найти:").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 6))
        self.find_entry = ttk.Entry(frame, textvariable=self.find_var, width=34)
        self.find_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(frame, text="Заменить на:").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 6))
        self.replace_entry = ttk.Entry(frame, textvariable=self.replace_var, width=34)
        self.replace_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Checkbutton(frame, text="Учитывать регистр",
                        variable=self.match_case_var).grid(row=2, column=1, sticky="w", pady=3)

        btns = ttk.Frame(frame)
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Найти далее", command=self.find_next).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Заменить", command=self.replace_current).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Заменить всё", command=self.replace_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Закрыть", command=self.destroy).pack(side=tk.RIGHT)

        ttk.Label(frame, textvariable=self.status_var,
                  foreground=THEME["muted"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.find_entry.bind("<Return>", lambda e: self.find_next())
        self.replace_entry.bind("<Return>", lambda e: self.replace_current())
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # Начальное значение — текущее выделение (если оно в одну строку).
        selection = self._selection_text()
        if selection and "\n" not in selection:
            self.find_var.set(selection)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, tk.END)

    # ---------- вспомогательные ----------
    def _selection_text(self):
        try:
            return self.text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return ""

    def _nocase(self):
        return not self.match_case_var.get()

    def _search_from(self, start):
        needle = self.find_var.get()
        if not needle:
            return None
        return self.text.search(needle, start, stopindex=tk.END, nocase=self._nocase()) or None

    # ---------- действия ----------
    def find_next(self, event=None):
        needle = self.find_var.get()
        if not needle:
            self.status_var.set("Введите текст для поиска.")
            return
        idx = self._search_from(self.text.index(tk.INSERT))
        if idx is None:                      # продолжить поиск с начала текста
            idx = self._search_from("1.0")
        if idx is None:
            self.status_var.set("Больше не найдено.")
            self.bell()
            return
        end = f"{idx}+{len(needle)}c"
        self.text.tag_remove(tk.SEL, "1.0", tk.END)
        self.text.tag_add(tk.SEL, idx, end)
        self.text.mark_set(tk.INSERT, end)
        self.text.see(idx)
        self.status_var.set("")

    def replace_current(self, event=None):
        if self.text.cget("state") == tk.DISABLED:
            return
        needle = self.find_var.get()
        if not needle:
            self.status_var.set("Введите текст для поиска.")
            return
        selected = self._selection_text()
        matches = selected == needle or (self._nocase() and selected.lower() == needle.lower())
        if selected and matches:
            self.text.delete(tk.SEL_FIRST, tk.SEL_LAST)
            self.text.insert(tk.INSERT, self.replace_var.get())
        self.find_next()

    def replace_all(self, event=None):
        if self.text.cget("state") == tk.DISABLED:
            return
        needle = self.find_var.get()
        if not needle:
            self.status_var.set("Введите текст для поиска.")
            return
        replacement = self.replace_var.get()
        count = 0
        pos = "1.0"
        while count < 100000:
            idx = self._search_from(pos)
            if idx is None:
                break
            end = f"{idx}+{len(needle)}c"
            self.text.delete(idx, end)
            if replacement:
                self.text.insert(idx, replacement)
            pos = self.text.index(f"{idx}+{len(replacement)}c")
            count += 1
        self.text.tag_remove(tk.SEL, "1.0", tk.END)
        self.status_var.set(f"Заменено вхождений: {count}" if count else "Не найдено.")

# -------------------- Диалог для отображения ошибок с возможностью копирования --------------------
class ErrorDialog(tk.Toplevel):
    def __init__(self, parent, title, message):
        super().__init__(parent)
        self.title(title)
        self.geometry("700x400")
        self.minsize(500, 250)
        self.resizable(True, True)
        # Окно ошибки должно появляться там же, где главное окно, и быть поверх:
        # иначе на нескольких мониторах его легко не заметить.
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text=title, font=('', 10, 'bold')).pack(anchor=tk.W, pady=(0,5))

        text_frame = ttk.Frame(main_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(text_frame, wrap=tk.WORD, bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"])
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.insert(tk.END, message)
        self.text.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=(10, 0))
        ttk.Button(btn_frame, text="Копировать", command=self._copy_to_clipboard).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=self.destroy).pack(side=tk.LEFT, padx=5)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        install_text_editor_shortcuts(self.text, editable=False)
        self.text.focus_set()
        maximize_window(self)
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _copy_to_clipboard(self):
        text = self.text.get(1.0, tk.END).strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.text.configure(state=tk.NORMAL)
        self.text.tag_add("sel", "1.0", tk.END)
        self.text.after(500, lambda: self.text.tag_remove("sel", "1.0", tk.END))
        self.text.configure(state=tk.DISABLED)

# -------------------- Кастомный диалог подтверждения удаления --------------------
class ConfirmDeleteDialog(tk.Toplevel):
    def __init__(self, parent, files_to_delete, templates_to_delete, nodes_count):
        super().__init__(parent)
        self.title("Подтверждение удаления")
        self.geometry("800x550")
        self.minsize(600, 400)
        self.resizable(True, True)
        self.result = False

        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="Вы собираетесь удалить следующие элементы:", font=('', 10, 'bold')).pack(anchor=tk.W, pady=(0,5))

        columns = ("type", "name", "details")
        tree = ttk.Treeview(main_frame, columns=columns, show="headings", selectmode=tk.NONE)
        tree.heading("type", text="Тип")
        tree.heading("name", text="Имя")
        tree.heading("details", text="Детали")
        tree.column("type", width=80, anchor=tk.CENTER)
        tree.column("name", width=250)
        tree.column("details", width=300)

        scrollbar = ttk.Scrollbar(main_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for f in files_to_delete:
            tree.insert("", tk.END, values=("Образ", f.name, f"Путь: {f}"))
        for t in templates_to_delete:
            tree.insert("", tk.END, values=("Шаблон", t['name'], f"ID: {t['template_id']}"))
        if nodes_count > 0:
            tree.insert("", tk.END, values=("Узлы (машины)", f"{nodes_count} шт.", "Будут остановлены и удалены из проектов"))

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=(10, 0))
        ttk.Button(btn_frame, text="Удалить", command=self._on_delete).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=10)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)

    def _on_delete(self):
        self.result = True
        self.destroy()

# -------------------- Кастомный диалог выбора файла (изменён для поддержки всех файлов) --------------------
class FileSelectorDialog(tk.Toplevel):
    def __init__(self, parent, initial_dir, title="Выберите файл", extensions=None):
        """
        extensions: набор расширений для фильтрации (например, {'.qcow2', '.img'}).
                    Если None — показывать все файлы.
        """
        super().__init__(parent)
        self.title(title)
        self.geometry("950x700")
        self.minsize(700, 500)
        self.result = None
        self.current_dir = Path(initial_dir) if initial_dir and Path(initial_dir).exists() else Path.home()
        self.extensions = set(extensions) if extensions is not None else None
        self.sort_col = "name"      # текущая колонка сортировки
        self.sort_reverse = False

        # --- навигация ---
        nav_frame = ttk.Frame(self)
        nav_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(nav_frame, text="Вверх", command=self._go_up).pack(side=tk.LEFT, padx=5)
        self.dir_var = tk.StringVar(value=str(self.current_dir))
        path_entry = ttk.Entry(nav_frame, textvariable=self.dir_var, width=60)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        path_entry.bind("<Return>", lambda event: self._browse_to())
        ttk.Button(nav_frame, text="Перейти", command=self._browse_to).pack(side=tk.LEFT, padx=5)

        # --- таблица ---
        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("name", "size", "type", "modified")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode=tk.BROWSE)
        self.tree.heading("name", text="Имя", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Размер", command=lambda: self._sort_by("size"))
        self.tree.heading("type", text="Тип", command=lambda: self._sort_by("type"))
        self.tree.heading("modified", text="Дата изменения", command=lambda: self._sort_by("modified"))

        self.tree.column("name", width=350)
        self.tree.column("size", width=100, anchor=tk.CENTER)
        self.tree.column("type", width=80, anchor=tk.CENTER)
        self.tree.column("modified", width=150, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.bind("<Escape>", lambda event: self.destroy())

        # --- кнопки ---
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=5, padx=10, fill=tk.X)
        ttk.Button(btn_frame, text="Выбрать", command=self._on_select).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.RIGHT, padx=5)

        self._refresh_list()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)

    def _refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        try:
            entries = list(self.current_dir.iterdir())
        except PermissionError:
            return

        dirs = []
        files = []
        for entry in entries:
            if entry.is_dir():
                dirs.append((entry.name, "", "<Каталог>", "", entry))
            else:
                if self.extensions is not None and entry.suffix.lower() not in self.extensions:
                    continue
                stat = entry.stat()
                size = stat.st_size
                size_str = f"{size/1024/1024:.1f} МБ" if size > 1024*1024 else f"{size/1024:.1f} КБ"
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                files.append((entry.name, size_str, entry.suffix.upper(), mtime, entry))

        # Сортируем каталоги и файлы отдельно (каталоги всегда сверху)
        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())

        # Применяем сортировку по выбранной колонке (только для файлов, каталоги остаются сверху)
        if self.sort_col != "name":  # сортировка по другим колонкам применяется к файлам
            if self.sort_col == "size":
                # размер в байтах для сортировки (извлекаем из записи)
                files.sort(key=lambda x: x[4].stat().st_size, reverse=self.sort_reverse)
            elif self.sort_col == "type":
                files.sort(key=lambda x: x[2].lower(), reverse=self.sort_reverse)
            elif self.sort_col == "modified":
                files.sort(key=lambda x: x[3], reverse=self.sort_reverse)
        else:
            # по имени уже отсортированы, но если reverse, то инвертируем
            if self.sort_reverse:
                files.reverse()

        # Вставляем каталоги, затем файлы
        for item in dirs:
            self.tree.insert("", tk.END, values=item[:4], tags=("dir",))
        for item in files:
            self.tree.insert("", tk.END, values=item[:4], tags=("file",))

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False
        self._refresh_list()


    def _on_double_click(self, event):
        item = self.tree.focus()
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return
        name = values[0]
        full_path = self.current_dir / name
        if full_path.is_dir():
            self.current_dir = full_path
            self.dir_var.set(str(self.current_dir))
            self._refresh_list()
        else:
            self.result = str(full_path)
            self.destroy()

    def _on_select(self):
        item = self.tree.focus()
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return
        name = values[0]
        full_path = self.current_dir / name
        if full_path.is_dir():
            self.current_dir = full_path
            self.dir_var.set(str(self.current_dir))
            self._refresh_list()
        else:
            self.result = str(full_path)
            self.destroy()

    def _go_up(self):
        self.current_dir = self.current_dir.parent
        self.dir_var.set(str(self.current_dir))
        self._refresh_list()

    def _browse_to(self):
        new_path = Path(self.dir_var.get())
        if new_path.exists() and new_path.is_dir():
            self.current_dir = new_path
            self._refresh_list()
        else:
            ErrorDialog(self, "Ошибка", "Папка не существует")

# -------------------- Диалог настроек --------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config, callback):
        super().__init__(parent)
        self.config = config
        self.callback = callback
        self.title("Настройки GNS3")
        self.geometry("600x700")
        self.minsize(500, 600)

        canvas = tk.Canvas(self, bg=THEME["background"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        main_frame = ttk.Frame(scroll_frame, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(main_frame, text="URL сервера GNS3:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.host_var = tk.StringVar(value=config.get("gns3_host", ""))
        ttk.Entry(main_frame, textvariable=self.host_var, width=30).grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        row += 1

        ttk.Label(main_frame, text="Логин:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.user_var = tk.StringVar(value=config.get("gns3_user", ""))
        ttk.Entry(main_frame, textvariable=self.user_var, width=30).grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        row += 1

        ttk.Label(main_frame, text="Пароль:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.pass_var = tk.StringVar(value=config.get("gns3_pass", ""))
        ttk.Entry(main_frame, textvariable=self.pass_var, width=30, show="*").grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        row += 1

        ttk.Label(main_frame, text="Путь к QEMU:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.qemu_var = tk.StringVar(value=config.get("qemu_path", ""))
        ttk.Entry(main_frame, textvariable=self.qemu_var, width=30).grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        row += 1

        ttk.Label(main_frame, text="RAM по умолч. (МБ):").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.ram_var = tk.IntVar(value=config.get("default_ram", 256))
        ttk.Entry(main_frame, textvariable=self.ram_var, width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="vCPUs по умолч.:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.cpu_var = tk.IntVar(value=config.get("default_cpus", 1))
        ttk.Entry(main_frame, textvariable=self.cpu_var, width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Адаптеров по умолч.:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.adapters_var = tk.IntVar(value=config.get("adapters", 1))
        ttk.Spinbox(main_frame, from_=1, to=99, textvariable=self.adapters_var, width=5).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Тип адаптера:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.adapter_var = tk.StringVar(value=config.get("adapter_type", "e1000"))
        adapters = ["e1000", "virtio-net-pci", "rtl8139", "pcnet"]
        ttk.Combobox(main_frame, textvariable=self.adapter_var, values=adapters, state="readonly", width=18).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Приоритет загрузки:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.boot_var = tk.StringVar(value=config.get("boot_priority", "c"))
        boot_opts = ["c", "d", "cd", "dc", "n"]
        ttk.Combobox(main_frame, textvariable=self.boot_var, values=boot_opts, state="readonly", width=5).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Тип консоли:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.console_var = tk.StringVar(value=config.get("console_type", "telnet"))
        consoles = ["telnet", "vnc", "spice", "none"]
        ttk.Combobox(main_frame, textvariable=self.console_var, values=consoles, state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Platform:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.platform_var = tk.StringVar(value=config.get("platform", "x86_64"))
        platforms = ["x86_64", "i386", "arm", "aarch64", "ppc64"]
        ttk.Combobox(main_frame, textvariable=self.platform_var, values=platforms, state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Символ (SVG) по умолчанию:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.symbol_var = tk.StringVar(value=config.get("default_symbol", ""))
        ttk.Entry(main_frame, textvariable=self.symbol_var, width=30).grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        row += 1

        ttk.Label(main_frame, text="Категория по умолчанию:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.category_var = tk.StringVar(value=config.get("default_category", "router"))
        categories = ["router", "switch", "guest", "firewall"]
        ttk.Combobox(main_frame, textvariable=self.category_var, values=categories, width=18).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Папка для импорта образов:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.import_dir_var = tk.StringVar(value=config.get("import_dir", DEFAULT_IMPORT_DIR))
        import_frame = ttk.Frame(main_frame)
        import_frame.grid(row=row, column=1, sticky="ew", padx=5, pady=5)
        ttk.Entry(import_frame, textvariable=self.import_dir_var, width=30).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(import_frame, text="Обзор", command=self._browse_import_dir).pack(side=tk.LEFT, padx=5)
        row += 1

        ttk.Label(main_frame, text="Интерфейс HDA по умолч.:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.hda_iface_var = tk.StringVar(value=config.get("hda_disk_interface", "sata"))
        ifaces = ["ide", "sata", "scsi", "virtio", "none"]
        ttk.Combobox(main_frame, textvariable=self.hda_iface_var, values=ifaces, state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(main_frame, text="Интерфейс HDB по умолч.:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.hdb_iface_var = tk.StringVar(value=config.get("hdb_disk_interface", "sata"))
        ttk.Combobox(main_frame, textvariable=self.hdb_iface_var, values=ifaces, state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        self.linked_clone_var = tk.BooleanVar(value=config.get("linked_clone", True))
        ttk.Label(main_frame, text="Связанные клоны по умолчанию:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        ttk.Checkbutton(main_frame, text="Использовать linked clone", variable=self.linked_clone_var).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="Сохранить", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=5)
        main_frame.columnconfigure(1, weight=1)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)

    def _browse_import_dir(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Выберите папку для импорта образов")
        if path:
            self.import_dir_var.set(path)

    def save(self):
        self.config["gns3_host"] = self.host_var.get().rstrip("/")
        self.config["gns3_user"] = self.user_var.get()
        self.config["gns3_pass"] = self.pass_var.get()
        self.config["qemu_path"] = self.qemu_var.get()
        self.config["default_ram"] = self.ram_var.get()
        self.config["default_cpus"] = self.cpu_var.get()
        self.config["adapters"] = self.adapters_var.get()
        self.config["adapter_type"] = self.adapter_var.get()
        self.config["boot_priority"] = self.boot_var.get()
        self.config["console_type"] = self.console_var.get()
        self.config["platform"] = self.platform_var.get()
        self.config["default_symbol"] = self.symbol_var.get()
        self.config["default_category"] = self.category_var.get()
        self.config["import_dir"] = self.import_dir_var.get()
        self.config["hda_disk_interface"] = self.hda_iface_var.get()
        self.config["hdb_disk_interface"] = self.hdb_iface_var.get()
        self.config["linked_clone"] = self.linked_clone_var.get()
        save_config(self.config)
        self.callback()
        self.destroy()

# -------------------- Диалог редактора конфигурационного ISO (с загрузкой из ISO) --------------------
class ConfigISOEditorDialog(tk.Toplevel):
    def __init__(self, parent, config, iso_path=None):
        """
        iso_path: если указан, открывает редактор для редактирования этого ISO
        """
        super().__init__(parent)
        self.config = config
        self.editing_iso_path = iso_path   # путь к редактируемому ISO, если есть
        self.result_path = None            # путь к сохранённому ISO (для обновления списка)

        title = "Редактирование конфигурационного ISO" if iso_path else "Создание конфигурационного ISO"
        self.title(title)
        self.geometry("800x650")
        self.minsize(600, 450)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # ---------- Вкладка "Общие" ----------
        common_frame = ttk.Frame(notebook, padding=10)
        notebook.add(common_frame, text="Общие")

        row = 0
        ttk.Label(common_frame, text="Тип устройства:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.types = [("NGFW nocloud", "ngfw"), ("Cisco config", "cisco"), ("MikroTik script", "mikrotik")]
        self.type_var = tk.StringVar(value=self.types[0][0])
        ttk.Combobox(common_frame, textvariable=self.type_var, values=[t[0] for t in self.types],
                     state="readonly", width=20).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        self.type_var.trace("w", lambda *a: self._switch_type())
        row += 1

        ttk.Label(common_frame, text="Имя ISO файла:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.iso_name_var = tk.StringVar(value="config.iso")
        ttk.Entry(common_frame, textvariable=self.iso_name_var, width=30).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        ttk.Label(common_frame, text="Папка сохранения:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.dir_var = tk.StringVar(value=str(CONFIG_ISO_DIR))
        ttk.Entry(common_frame, textvariable=self.dir_var, width=30).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        row += 1

        # Кнопка загрузки из ISO (для ручного открытия другого файла)
        load_btn = ttk.Button(common_frame, text="Загрузить из ISO...", command=self._load_from_iso)
        load_btn.grid(row=row, column=0, columnspan=2, pady=10)

        # ---------- Вкладка "Содержимое" ----------
        self.content_notebook = ttk.Notebook(notebook)
        notebook.add(self.content_notebook, text="Содержимое")

        self.user_data_text = None
        self.meta_data_text = None
        self.vendor_data_text = None
        self.config_text = None
        self.config_file_name_var = tk.StringVar(value="startup-config.cfg")

        self._build_content_tabs()
        self.grab_set()
        # Фокус сразу в поле конфига: горячие клавиши (Ctrl+Z, Ctrl+A) работают
        # без предварительного клика по тексту.
        editor = self.config_text or self.user_data_text
        if editor is not None:
            self.after_idle(editor.focus_set)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # ---------- Кнопки внизу ----------
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(0, 10))

        if self.editing_iso_path:
            # Кнопка "Сохранить" — перезаписать исходный файл
            self.save_btn = ttk.Button(btn_frame, text="Сохранить (перезаписать)", command=self._save_existing)
            self.save_btn.pack(side=tk.LEFT, padx=10)

        self.create_btn = ttk.Button(btn_frame, text="Создать ISO", command=self._create_new)
        self.create_btn.pack(side=tk.LEFT, padx=10)

        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=10)

        # Если передан путь — загружаем его содержимое
        if iso_path:
            self._load_from_path(iso_path)
            self.iso_name_var.set(Path(iso_path).name)

        maximize_window(self)

    # ---------- Вспомогательные методы ----------
    def _build_content_tabs(self):
        """Строит вкладки в зависимости от текущего типа."""
        for child in self.content_notebook.winfo_children():
            child.destroy()

        if self.type_var.get().startswith("NGFW"):
            # user-data
            tab_user = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_user, text="user-data")
            ttk.Label(tab_user, text="user-data (YAML):", font=("", 9, "bold")).pack(anchor="w")
            self.user_data_text = tk.Text(tab_user, wrap="word", bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"])
            self.user_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.user_data_text.insert("1.0", "#cloud-config\npassword: admin\nchpasswd: { expire: False }\nssh_pwauth: true\n")
            install_text_editor_shortcuts(self.user_data_text, save_callback=self._save_from_shortcut)

            # meta-data
            tab_meta = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_meta, text="meta-data")
            ttk.Label(tab_meta, text="meta-data:", font=("", 9, "bold")).pack(anchor="w")
            self.meta_data_text = tk.Text(tab_meta, wrap="word", bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"], height=5)
            self.meta_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.meta_data_text.insert("1.0", "instance-id: i-ngfw\nlocal-hostname: ngfw\n")
            install_text_editor_shortcuts(self.meta_data_text, save_callback=self._save_from_shortcut)

            # vendor-data
            tab_vendor = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_vendor, text="vendor-data")
            ttk.Label(tab_vendor, text="vendor-data (опционально):", font=("", 9, "bold")).pack(anchor="w")
            self.vendor_data_text = tk.Text(tab_vendor, wrap="word", bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"])
            self.vendor_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.vendor_data_text.insert("1.0", "")
            install_text_editor_shortcuts(self.vendor_data_text, save_callback=self._save_from_shortcut)
        else:
            # Обычный конфигурационный файл
            tab_file = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_file, text="Конфигурационный файл")

            frame_name = ttk.Frame(tab_file)
            frame_name.pack(fill=tk.X, pady=5)
            ttk.Label(frame_name, text="Имя файла внутри ISO:").pack(side=tk.LEFT)
            ttk.Entry(frame_name, textvariable=self.config_file_name_var, width=30).pack(side=tk.LEFT, padx=5)

            self.config_text = tk.Text(tab_file, wrap="word", bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"])
            self.config_text.pack(fill=tk.BOTH, expand=True, pady=5)
            if self.type_var.get().startswith("Cisco"):
                self.config_text.insert("1.0", "!\nhostname Router\n!\n")
            else:
                self.config_text.insert("1.0", "/system identity set name=MyRouter\n")
            install_text_editor_shortcuts(self.config_text, save_callback=self._save_from_shortcut)

    def _switch_type(self):
        self._build_content_tabs()

    def _save_from_shortcut(self):
        """Ctrl+S — сохранить ISO так же, как кнопками внизу окна."""
        if self.editing_iso_path:
            self._save_existing()
        else:
            self._create_new()

    def _load_from_path(self, path):
        """Загружает содержимое ISO из указанного пути и заполняет поля."""
        try:
            content_dict = read_iso_content(path)
        except Exception as e:
            ErrorDialog(self, "Ошибка чтения ISO", str(e))
            return

        # Проверяем nocloud
        if 'user-data' in content_dict and 'meta-data' in content_dict:
            self.type_var.set("NGFW nocloud")
            self._switch_type()
            if self.user_data_text:
                self.user_data_text.delete("1.0", tk.END)
                self.user_data_text.insert("1.0", content_dict.get('user-data', ''))
            if self.meta_data_text:
                self.meta_data_text.delete("1.0", tk.END)
                self.meta_data_text.insert("1.0", content_dict.get('meta-data', ''))
            if self.vendor_data_text:
                self.vendor_data_text.delete("1.0", tk.END)
                self.vendor_data_text.insert("1.0", content_dict.get('vendor-data', ''))
        else:
            # Generic ISO: берём первый файл
            files = [f for f in content_dict.keys() if f not in ('.', '..')]
            if not files:
                ErrorDialog(self, "Ошибка", "В ISO не найдено файлов с данными.")
                return
            chosen = files[0]
            data = content_dict[chosen]
            # Автоопределение типа
            if 'hostname' in data.lower() or 'interface' in data.lower():
                self.type_var.set("Cisco config")
            elif '/system' in data.lower() or 'identity' in data.lower():
                self.type_var.set("MikroTik script")
            else:
                self.type_var.set("Cisco config")
            self._switch_type()
            if self.config_text:
                self.config_text.delete("1.0", tk.END)
                self.config_text.insert("1.0", data)
            self.config_file_name_var.set(chosen)

        self.deiconify()
        self.lift()
        self.focus_force()

    def _load_from_iso(self):
        """Загружает из другого ISO (кнопка)."""
        dlg = FileSelectorDialog(self, initial_dir=str(CONFIG_ISO_DIR),
                                 title="Выберите ISO для загрузки", extensions=['.iso'])
        self.wait_window(dlg)
        if dlg.result:
            self._load_from_path(dlg.result)
            self.iso_name_var.set(Path(dlg.result).name)

    def _save_to_path(self, output_path):
        """Сохраняет текущее содержимое в указанный путь (создаёт ISO)."""
        # Проверка mkisofs
        if MKISOFS_CMD is None:
            raise RuntimeError("Утилита mkisofs/genisoimage не установлена.\n"
                               "Установите её (apt install genisoimage или аналогичное).")

        if self.type_var.get().startswith("NGFW"):
            user_data = self.user_data_text.get("1.0", tk.END).strip()
            meta_data = self.meta_data_text.get("1.0", tk.END).strip()
            vendor_data = self.vendor_data_text.get("1.0", tk.END).strip()
            create_nocloud_iso(str(output_path), user_data, meta_data, vendor_data)
        else:
            file_name = self.config_file_name_var.get().strip()
            content = self.config_text.get("1.0", tk.END).strip()
            vol_id = "CISCO" if "cisco" in self.type_var.get().lower() else "MIKROTIK"
            create_generic_config_iso(str(output_path), file_name, content, vol_id)

    def _save_existing(self):
        """Перезаписывает исходный ISO (если в режиме редактирования)."""
        if not self.editing_iso_path:
            return
        if not messagebox.askyesno("Подтверждение",
                                   f"Перезаписать существующий ISO?\n{self.editing_iso_path}",
                                   parent=self):
            return
        try:
            self._save_to_path(self.editing_iso_path)
        except Exception as e:
            ErrorDialog(self, "Ошибка сохранения", str(e))
            return
        self.result_path = self.editing_iso_path
        messagebox.showinfo("Готово", f"ISO перезаписан:\n{self.editing_iso_path}", parent=self)
        self.destroy()

    def _create_new(self):
        """Создаёт новый ISO с именем из поля (или сохраняет как)."""
        iso_name = self.iso_name_var.get().strip()
        if not iso_name:
            ErrorDialog(self, "Ошибка", "Введите имя ISO-файла.")
            return
        if not iso_name.endswith(".iso"):
            iso_name += ".iso"
        out_dir = Path(self.dir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / iso_name

        # Если файл уже существует и это не тот же файл, что редактируем, спросим
        if output_path.exists() and (not self.editing_iso_path or output_path != self.editing_iso_path):
            if not messagebox.askyesno("Файл существует",
                                       f"Файл {output_path.name} уже существует. Заменить?",
                                       parent=self):
                return

        try:
            self._save_to_path(output_path)
        except Exception as e:
            ErrorDialog(self, "Ошибка создания ISO", str(e))
            return
        self.result_path = str(output_path)
        messagebox.showinfo("Готово", f"ISO создан:\n{output_path}", parent=self)
        self.destroy()

# -------------------- Диалог управления конфигурационными ISO --------------------
class ConfigISOManagerDialog(tk.Toplevel):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.title("Управление конфигурационными ISO")
        self.geometry("750x550")
        self.minsize(500, 350)

        top_frame = ttk.Frame(self, padding=5)
        top_frame.pack(fill=tk.X)

        ttk.Button(top_frame, text="Создать новый ISO", command=self._create_iso).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="Редактировать выбранный", command=self._edit_iso).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="Удалить выбранный", command=self._delete_iso).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="Обновить список", command=self._refresh).pack(side=tk.LEFT, padx=5)

        columns = ("name", "size", "modified")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode=tk.BROWSE)
        self.tree.heading("name", text="Имя файла")
        self.tree.heading("size", text="Размер (КБ)")
        self.tree.heading("modified", text="Изменён")
        self.tree.column("name", width=350)
        self.tree.column("size", width=100, anchor=tk.CENTER)
        self.tree.column("modified", width=150, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<ButtonRelease-1>", self._open_iso_on_click)
        self.tree.bind("<Return>", lambda event: self._edit_iso())

        self._refresh()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not CONFIG_ISO_DIR.exists():
            return
        for entry in CONFIG_ISO_DIR.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".iso":
                stat = entry.stat()
                size_kb = stat.st_size // 1024
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                self.tree.insert("", tk.END, values=(entry.name, size_kb, mtime), tags=(str(entry),))

    def _open_iso_on_click(self, event):
        """Открывает редактор по щелчку именно по строке таблицы, а не по пустому месту."""
        if self.tree.identify_row(event.y):
            self.after_idle(self._edit_iso)

    def _create_iso(self):
        dlg = ConfigISOEditorDialog(self, self.config)
        self.wait_window(dlg)
        if dlg.result_path:
            self._refresh()

    def _edit_iso(self):
        sel = self.tree.selection()
        if not sel:
            ErrorDialog(self, "Ошибка", "Выберите ISO для редактирования.")
            return
        path_str = self.tree.item(sel[0], "tags")[0]
        path = Path(path_str)
        if not path.exists():
            ErrorDialog(self, "Ошибка", "Файл не найден.")
            return
        dlg = ConfigISOEditorDialog(self, self.config, iso_path=str(path))
        self.wait_window(dlg)
        if dlg.result_path:   # если что-то сохранили (перезаписали или создали новое)
            self._refresh()

    def _delete_iso(self):
        sel = self.tree.selection()
        if not sel:
            ErrorDialog(self, "Ошибка", "Выберите ISO для удаления.")
            return
        path = self.tree.item(sel[0], "tags")[0]
        if messagebox.askyesno("Подтверждение", f"Удалить {Path(path).name}?", parent=self):
            try:
                os.remove(path)
            except Exception as e:
                ErrorDialog(self, "Ошибка", str(e))
            self._refresh()

# -------------------- Диалог создания шаблона (с кнопкой для ISO) --------------------
class CreateTemplateDialog(tk.Toplevel):
    def __init__(self, parent, image_path, config, on_submit=None):
        super().__init__(parent)
        self.image_path = image_path
        self.config = config
        self.result = None
        # Создание выполняет главное окно по этому колбэку: так шаблон создаётся
        # даже если после закрытия окна что-то пойдёт не так (раньше при сбое
        # после открытия окна запрос в GNS3 вообще не отправлялся).
        self.on_submit = on_submit
        # Имя образа в заголовке — чтобы всегда было видно, для какого образа создаётся шаблон.
        self.title(f"Создать шаблон QEMU в GNS3 — {Path(image_path).name}")
        self.geometry("700x600")
        self.resizable(True, True)
        self.minsize(600, 500)
        # Привязываем окно к главному: оконный менеджер открывает его на том же
        # мониторе, что и главное окно, поверх него и разворачивает на весь экран.
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tab_basic = ttk.Frame(notebook); notebook.add(tab_basic, text="Основные"); self._create_basic_tab(tab_basic)
        tab_disks = ttk.Frame(notebook); notebook.add(tab_disks, text="Диски"); self._create_disks_tab(tab_disks)
        tab_net = ttk.Frame(notebook); notebook.add(tab_net, text="Сеть"); self._create_network_tab(tab_net)
        tab_adv = ttk.Frame(notebook); notebook.add(tab_adv, text="Дополнительно"); self._create_advanced_tab(tab_adv)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(0, 10))
        create_btn = ttk.Button(btn_frame, text="Создать", command=self.create)
        create_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=5)
        # Enter в окне создания = «Создать» (как в обычных диалогах)
        self.bind("<Return>", lambda event: self.create())
        self.hda_var.set(image_path)

        self._grab_ok = False
        self._try_set_grab()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _try_set_grab(self):
        """Устанавливает модальность; при неудаче повторяет после появления окна.

        В Tk/X11 `grab_set()` до отображения окна может завершиться ошибкой
        «grab failed: window not viewable». Если это не перехватить, исключение
        прерывало открытие окна и создание шаблона не происходило.
        """
        try:
            self.grab_set()
            self._grab_ok = True
        except tk.TclError as e:
            log_event(f"grab_set не удался ({e}), повторю после отображения окна")
            try:
                self.after(200, self._try_set_grab)
            except tk.TclError:
                pass

    def _create_basic_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        auto_name = generate_template_name(self.image_path)
        r = 0
        ttk.Label(frame, text="Имя шаблона:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.name_var = tk.StringVar(value=auto_name)
        ttk.Entry(frame, textvariable=self.name_var, width=35).grid(row=r, column=1, padx=5, pady=5, sticky="ew"); r += 1
        ttk.Label(frame, text="RAM (МБ):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.ram_var = tk.IntVar(value=self.config.get("default_ram", 256))
        ttk.Entry(frame, textvariable=self.ram_var, width=10).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="vCPUs:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.cpu_var = tk.IntVar(value=self.config.get("default_cpus", 1))
        ttk.Entry(frame, textvariable=self.cpu_var, width=10).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="QEMU binary:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.qemu_var = tk.StringVar(value=self.config.get("qemu_path", ""))
        ttk.Entry(frame, textvariable=self.qemu_var, width=35).grid(row=r, column=1, padx=5, pady=5, sticky="ew"); r += 1
        ttk.Label(frame, text="Platform:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.platform_var = tk.StringVar(value=self.config.get("platform", "x86_64"))
        ttk.Combobox(frame, textvariable=self.platform_var, values=["x86_64","i386","arm","aarch64","ppc64"], state="readonly", width=15).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="Приоритет загрузки:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.boot_var = tk.StringVar(value=self.config.get("boot_priority", "c"))
        ttk.Combobox(frame, textvariable=self.boot_var, values=["c","d","cd","dc","n"], state="readonly", width=5).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="Тип консоли:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.console_var = tk.StringVar(value=self.config.get("console_type", "telnet"))
        ttk.Combobox(frame, textvariable=self.console_var, values=["telnet","vnc","spice","none"], state="readonly", width=10).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="Символ (SVG):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.symbol_var = tk.StringVar(value=self.config.get("default_symbol", ""))
        ttk.Entry(frame, textvariable=self.symbol_var, width=35).grid(row=r, column=1, padx=5, pady=5, sticky="ew"); r += 1
        ttk.Label(frame, text="Категория:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.category_var = tk.StringVar(value=self.config.get("default_category", "router"))
        ttk.Combobox(frame, textvariable=self.category_var, values=["router", "switch", "guest", "firewall"], width=20).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1

        ttk.Label(frame, text="Конфигурационный ISO (cloud-init):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.cdrom_var = tk.StringVar()
        self.config_iso_choices = self._get_config_iso_choices()
        cdrom_frame = ttk.Frame(frame)
        cdrom_frame.grid(row=r, column=1, sticky="ew", padx=5, pady=5)
        self.cdrom_combo = ttk.Combobox(cdrom_frame, textvariable=self.cdrom_var,
                                        values=list(self.config_iso_choices), state="readonly", width=35)
        self.cdrom_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cdrom_frame, text="Обновить", command=self._refresh_config_iso_choices).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(cdrom_frame, text="Обзор", command=self._browse_cdrom).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(cdrom_frame, text="Редактировать ISO", command=self._edit_selected_config_iso).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(cdrom_frame, text="Новый ISO", command=self._create_config_iso).pack(side=tk.LEFT, padx=(5, 0))
        frame.columnconfigure(1, weight=1)

    def _create_disks_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ifaces = ["ide", "sata", "scsi", "virtio", "none"]
        r = 0
        ttk.Label(frame, text="HDA (главный диск):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.hda_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.hda_var, width=30).grid(row=r, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(frame, text="Обзор", command=lambda: self._browse_file(self.hda_var)).grid(row=r, column=2, padx=5)
        self.hda_iface_var = tk.StringVar(value=self.config.get("hda_disk_interface", "sata"))
        ttk.Combobox(frame, textvariable=self.hda_iface_var, values=ifaces, state="readonly", width=8).grid(row=r, column=3, padx=5)
        r += 1

        ttk.Label(frame, text="HDB (второй диск):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.hdb_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.hdb_var, width=30).grid(row=r, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(frame, text="Обзор", command=lambda: self._browse_file(self.hdb_var)).grid(row=r, column=2, padx=5)
        self.hdb_iface_var = tk.StringVar(value=self.config.get("hdb_disk_interface", "sata"))
        ttk.Combobox(frame, textvariable=self.hdb_iface_var, values=ifaces, state="readonly", width=8).grid(row=r, column=3, padx=5)
        r += 1

        ttk.Label(frame, text="* Пути могут быть абсолютными или относительными (от images/QEMU)").grid(row=r, column=0, columnspan=4, sticky="w", padx=5, pady=10)
        frame.columnconfigure(1, weight=1)

    def _browse_file(self, stringvar):
        initial_dir = str(Path(stringvar.get()).parent) if stringvar.get() else str(TARGET_DIR)
        dlg = FileSelectorDialog(self, initial_dir=initial_dir,
                                 title="Выберите файл образа", extensions=IMAGE_EXTENSIONS)
        self.wait_window(dlg)
        if dlg.result:
            stringvar.set(dlg.result)

    def _get_config_iso_choices(self):
        if not CONFIG_ISO_DIR.exists():
            return {}
        return {entry.name: str(entry) for entry in sorted(CONFIG_ISO_DIR.iterdir(), key=lambda p: p.name.lower())
                if entry.is_file() and entry.suffix.lower() == ".iso"}

    def _refresh_config_iso_choices(self):
        self.config_iso_choices = self._get_config_iso_choices()
        self.cdrom_combo.configure(values=list(self.config_iso_choices))

    def _selected_cdrom_path(self):
        value = self.cdrom_var.get().strip()
        return self.config_iso_choices.get(value, value)

    def _browse_cdrom(self):
        dlg = FileSelectorDialog(self, initial_dir=str(CONFIG_ISO_DIR), title="Выберите ISO-образ", extensions=['.iso'])
        self.wait_window(dlg)
        if dlg.result:
            self.cdrom_var.set(dlg.result)

    def _create_config_iso(self):
        dlg = ConfigISOEditorDialog(self, self.config)
        self.wait_window(dlg)
        if dlg.result_path:
            self._refresh_config_iso_choices()
            self.cdrom_var.set(Path(dlg.result_path).name if Path(dlg.result_path).parent == CONFIG_ISO_DIR else dlg.result_path)

    def _edit_selected_config_iso(self):
        iso_path = self._selected_cdrom_path()
        if not iso_path:
            ErrorDialog(self, "Выбор ISO", "Выберите конфигурационный ISO из списка, который нужно отредактировать.")
            return
        path = Path(iso_path)
        if not path.is_file() or path.suffix.lower() != ".iso":
            ErrorDialog(self, "Выбор ISO", "Выбранный файл ISO не найден. Обновите список или выберите файл через «Обзор».")
            return
        dlg = ConfigISOEditorDialog(self, self.config, iso_path=str(path))
        self.wait_window(dlg)
        if dlg.result_path:
            self._refresh_config_iso_choices()
            self.cdrom_var.set(Path(dlg.result_path).name if Path(dlg.result_path).parent == CONFIG_ISO_DIR else dlg.result_path)

    def _create_network_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        r = 0
        ttk.Label(frame, text="Количество адаптеров:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.adapters_var = tk.IntVar(value=self.config.get("adapters", 1))
        ttk.Spinbox(frame, from_=1, to=99, textvariable=self.adapters_var, width=5).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="Тип адаптера:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.adapter_type_var = tk.StringVar(value=self.config.get("adapter_type", "e1000"))
        ttk.Combobox(frame, textvariable=self.adapter_type_var, values=["e1000","virtio-net-pci","rtl8139","pcnet"], state="readonly", width=18).grid(row=r, column=1, sticky="w", padx=5, pady=5); r += 1
        ttk.Label(frame, text="MAC-адрес (пусто = авто):").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.mac_var = tk.StringVar(); ttk.Entry(frame, textvariable=self.mac_var, width=20).grid(row=r, column=1, sticky="w", padx=5, pady=5)

    def _create_advanced_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        r = 0
        ttk.Label(frame, text="Доп. опции QEMU:").grid(row=r, column=0, sticky="nw", padx=5, pady=5)
        text = tk.Text(frame, width=40, height=6, bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"])
        text.grid(row=r, column=1, padx=5, pady=5, sticky="ew"); text.insert("1.0", self.config.get("options", ""))
        install_text_editor_shortcuts(text)
        self.options_text = text; r += 1
        self.linked_clone_var = tk.BooleanVar(value=self.config.get("linked_clone", True))
        ttk.Checkbutton(frame, variable=self.linked_clone_var).grid(row=r, column=1, sticky="w")
        ttk.Label(frame, text="Linked base:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        r += 1
        ttk.Label(frame, text="(позволяет запускать несколько копий одного образа)", foreground="gray").grid(row=r, column=1, sticky="w", padx=5, pady=0)

    def create(self):
        """Собирает параметры и закрывает окно (создание выполняет главное окно)."""
        try:
            if not self.name_var.get().strip():
                ErrorDialog(self, "Ошибка", "Введите имя шаблона.")
                return
            if not self.hda_var.get().strip():
                ErrorDialog(self, "Ошибка", "Укажите HDA диск.")
                return
            if not self.qemu_var.get().strip():
                ErrorDialog(self, "Ошибка", "Укажите путь к QEMU.")
                return
            self.result = {
                "name": self.name_var.get().strip(), "ram": self.ram_var.get(), "cpus": self.cpu_var.get(),
                "qemu_path": self.qemu_var.get().strip(), "hda_disk_image": self.hda_var.get().strip(),
                "hdb_disk_image": self.hdb_var.get().strip(), "cdrom_image": self._selected_cdrom_path(),
                "boot_priority": self.boot_var.get(), "platform": self.platform_var.get(),
                "console_type": self.console_var.get(), "adapters": self.adapters_var.get(),
                "adapter_type": self.adapter_type_var.get(), "mac_address": self.mac_var.get().strip(),
                "options": self.options_text.get("1.0", tk.END).strip(), "linked_clone": self.linked_clone_var.get(),
                "symbol": self.symbol_var.get().strip(), "category": self.category_var.get().strip(),
                "hda_disk_interface": self.hda_iface_var.get(),
                "hdb_disk_interface": self.hdb_iface_var.get() if self.hdb_var.get().strip() else ""
            }
        except tk.TclError as e:
            log_event(f"ОШИБКА чтения полей окна создания: {e}")
            ErrorDialog(self, "Ошибка", f"Не удалось прочитать параметры шаблона: {e}")
            return
        log_event(f"Нажата кнопка «Создать»: образ={self.image_path}, имя={self.result['name']!r}")
        parent, callback, params = self.master, self.on_submit, self.result
        self.destroy()
        # Создание выполняем сразу после закрытия окна, не полагаясь на то,
        # что ожидающий поток доживёт до этого момента.
        if callback is not None:
            try:
                callback(params)
            except Exception:
                log_event("ОШИБКА при создании шаблона:\n" + traceback.format_exc())
                ErrorDialog(parent, "Ошибка", traceback.format_exc())

# -------------------- Главное окно --------------------
class GNS3ImageManager:
    def __init__(self, root):
        self.root = root
        self.root.title("GNS3 Image Manager")
        self.root.geometry("1200x750")
        self.root.minsize(900, 550)
        self.config = load_config()
        self.sort_column = "name"
        self.sort_reverse = False
        self.templates_by_image = None
        self.template_association_state = "Проверяется API…"
        self.vm_count_by_image = None
        self.vm_count_complete = False
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh_list())
        main_frame = ttk.Frame(root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(main_frame)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text="Образы QEMU", font=("", 16, "bold")).pack(side=tk.LEFT)
        self.api_status_var = tk.StringVar(value="●")
        ttk.Label(header, text="API GNS3:").pack(side=tk.RIGHT)
        self.api_status_label = ttk.Label(header, textvariable=self.api_status_var,
                                           foreground="red", font=("", 12))
        self.api_status_label.pack(side=tk.RIGHT, padx=(6, 2))

        toolbar = ttk.Frame(main_frame)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        images_group = ttk.LabelFrame(toolbar, text="Образы", padding=6)
        images_group.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(images_group, text="Добавить образ…", style="Accent.TButton",
                   command=self.add_image).pack(side=tk.LEFT, padx=(0, 5))
        self.create_template_button = ttk.Button(images_group, text="Создать шаблон",
                                                 command=self.create_template_for_selected,
                                                 state=tk.DISABLED)
        self.create_template_button.pack(side=tk.LEFT, padx=5)
        self.delete_button = ttk.Button(images_group, text="Удалить выбранные", style="Danger.TButton",
                                        command=self.delete_images, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=(5, 0))

        secondary_bar = ttk.Frame(main_frame)
        secondary_bar.pack(fill=tk.X, pady=(0, 8))
        tools_group = ttk.LabelFrame(secondary_bar, text="Инструменты", padding=6)
        tools_group.pack(side=tk.LEFT)
        ttk.Button(tools_group, text="Конфигурационные ISO", command=self.open_config_iso_manager).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(tools_group, text="Настройки", command=self.open_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(tools_group, text="Проверить API", command=self.check_api).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(tools_group, text="Удалить неиспользуемые…", style="Danger.TButton",
                   command=self.delete_unused_images).pack(side=tk.LEFT, padx=(12, 5))
        ttk.Button(tools_group, text="Удалить шаблоны без образов…", style="Danger.TButton",
                   command=self.delete_templates_with_missing_images).pack(side=tk.LEFT, padx=5)

        filter_frame = ttk.Frame(main_frame)
        filter_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(filter_frame, text="Поиск:").pack(side=tk.LEFT)
        self.filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        ttk.Button(filter_frame, text="Очистить", command=lambda: self.filter_var.set("")).pack(side=tk.LEFT)
        ttk.Button(filter_frame, text="Обновить", command=self.refresh_list).pack(side=tk.LEFT, padx=(8, 0))

        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("name", "size", "templates", "vms", "modified")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode=tk.EXTENDED)
        self.tree.heading("name", text="Имя файла", command=lambda: self.sort_by_column("name"))
        self.tree.heading("size", text="Размер (МБ)", command=lambda: self.sort_by_column("size"))
        self.tree.heading("templates", text="Привязанные шаблоны")
        self.tree.heading("vms", text="ВМ", command=lambda: self.sort_by_column("vms"))
        self.tree.heading("modified", text="Изменён", command=lambda: self.sort_by_column("modified"))
        self.tree.column("name", width=600)
        self.tree.column("size", width=120, anchor=tk.CENTER)
        self.tree.column("templates", width=260)
        self.tree.column("vms", width=70, anchor=tk.CENTER)
        self.tree.column("modified", width=160, anchor=tk.CENTER)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_changed)
        self.tree.bind("<Double-1>", self._on_image_double_click)
        self.root.bind("<Control-i>", lambda event: self.add_image())
        self.root.bind("<F5>", lambda event: self.refresh_list())
        self.tree.bind("<Delete>", lambda event: self.delete_images())

        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="Готово")
        self.vm_info_var = tk.StringVar()
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(status_frame, textvariable=self.vm_info_var, anchor=tk.E,
                  foreground="gray").pack(side=tk.RIGHT, padx=(10, 0))

        self.refresh_list()
        self.root.after(500, self.check_api_silent)
        maximize_window(self.root)

    def sort_by_column(self, col):
        if self.sort_column == col: self.sort_reverse = not self.sort_reverse
        else: self.sort_column = col; self.sort_reverse = False
        self.refresh_list()

    def refresh_list(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        if not TARGET_DIR.exists():
            ErrorDialog(self.root, "Ошибка", f"Папка {TARGET_DIR} не существует!")
            return
        files = []
        for entry in TARGET_DIR.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
                stat = entry.stat()
                size_mb = round(stat.st_size / (1024*1024), 2)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                files.append((entry.name, size_mb, mtime, entry))
        if self.sort_column == "name": files.sort(key=lambda x: x[0].lower(), reverse=self.sort_reverse)
        elif self.sort_column == "size": files.sort(key=lambda x: x[1], reverse=self.sort_reverse)
        elif self.sort_column == "modified": files.sort(key=lambda x: x[2], reverse=self.sort_reverse)
        elif self.sort_column == "vms":
            files.sort(
                key=lambda item: self.vm_count_by_image.get(item[0], -1)
                if self.vm_count_by_image is not None else -1,
                reverse=self.sort_reverse,
            )
        query = self.filter_var.get().strip().lower()
        if query:
            files = [item for item in files if query in item[0].lower()]
        for name, size, mtime, path in files:
            if self.templates_by_image is None:
                template_text = self.template_association_state
            else:
                template_names = self.templates_by_image.get(name, [])
                template_text = ", ".join(template_names) if template_names else "—"
            if self.vm_count_by_image is None:
                vm_text = "—"
            else:
                vm_count = self.vm_count_by_image.get(name, 0)
                vm_text = str(vm_count) if self.vm_count_complete else f"≥ {vm_count}"
            self.tree.insert("", tk.END, values=(name, size, template_text, vm_text, mtime), tags=(str(path),))
        total = len(files) if not query else sum(
            1 for entry in TARGET_DIR.iterdir()
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS
        )
        self.status_var.set(f"Показано образов: {len(files)} из {total}" if query else f"Образов в папке: {total}")
        if hasattr(self, "vm_info_var"):
            self.vm_info_var.set("≥ — минимум: часть проектов недоступна"
                                 if self.vm_count_by_image is not None and not self.vm_count_complete else "")
        self._on_selection_changed()

    def _on_selection_changed(self, event=None):
        count = len(self.tree.selection())
        self.delete_button.configure(state=tk.NORMAL if count else tk.DISABLED)
        self.create_template_button.configure(state=tk.NORMAL if count == 1 else tk.DISABLED)
        if count:
            suffix = "" if count == 1 else "а" if 2 <= count <= 4 else "ов"
            self.status_var.set(f"Выбрано образ{suffix}: {count}")

    def _on_image_double_click(self, event):
        """Открывает создание шаблона для строки, по которой сделан двойной щелчок.

        Важно: берём именно строку под курсором, а не текущее выделение. Выделение
        успевает сброситься при обновлении списка (F5, фильтр, автообновление после
        обращения к API) и может относиться к другому образу — тогда диалог
        открывался для чужого образа, а для нажатого шаблон не создавался.
        """
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return
        # Выравниваем выделение по нажатой строке, иначе кнопки «Создать шаблон»
        # и «Удалить выбранные» продолжат относиться к прежней строке.
        self.tree.selection_set(item)
        self.tree.focus(item)
        self._open_create_template_dialog(str(TARGET_DIR / values[0]))

    def create_template_for_selected(self):
        """Создание шаблона для выделенного образа (кнопка «Создать шаблон»)."""
        selected = self.tree.selection()
        if len(selected) != 1:
            ErrorDialog(self.root, "Выбор образа", "Выберите один образ, для которого нужно создать шаблон.")
            return
        values = self.tree.item(selected[0], "values")
        if values:
            self._open_create_template_dialog(str(TARGET_DIR / values[0]))

    def _open_create_template_dialog(self, image_path):
        """Открывает диалог создания шаблона, не допуская двух окон одновременно."""
        if getattr(self, "_template_dialog_open", False):
            log_event(f"Диалог создания уже открыт — повторный вызов для {image_path} проигнорирован")
            return
        self._template_dialog_open = True
        log_event(f"Открываю окно создания шаблона: {image_path}")
        try:
            self.create_template_for_image(image_path)
        finally:
            self._template_dialog_open = False

    def refresh_template_associations(self):
        """Загружает привязки образов к шаблонам одним запросом к API GNS3."""
        try:
            response = requests.get(f"{self.config['gns3_host']}/v2/templates",
                                    auth=self._auth(), timeout=5)
            if response.status_code != 200:
                self.templates_by_image = None
                self.template_association_state = "Недоступно"
                self.vm_count_by_image = None
                self.vm_count_complete = False
                self.refresh_list()
                return
            associations = {}
            images_by_template_id = {}
            for template in response.json():
                image_names = set()
                for key in ("hda_disk_image", "hdb_disk_image"):
                    image = template.get(key)
                    if image:
                        image_name = Path(image).name
                        image_names.add(image_name)
                        associations.setdefault(image_name, []).append(template.get("name", "Без имени"))
                if image_names:
                    images_by_template_id[template.get("template_id")] = image_names
            self.templates_by_image = associations
            self.template_association_state = ""
            self.vm_count_by_image, self.vm_count_complete = self._get_vm_counts(images_by_template_id)
        except Exception:
            self.templates_by_image = None
            self.template_association_state = "Недоступно"
            self.vm_count_by_image = None
            self.vm_count_complete = False
        self.refresh_list()

    def _get_vm_counts(self, images_by_template_id):
        """Считает ВМ по шаблонам, не открывая и не изменяя проекты."""
        counts = {image_name: 0 for images in images_by_template_id.values() for image_name in images}
        complete = True
        try:
            projects_response = requests.get(f"{self.config['gns3_host']}/v2/projects",
                                             auth=self._auth(), timeout=5)
            if projects_response.status_code != 200:
                return None, False
            projects = projects_response.json()
        except Exception:
            return None, False

        for project in projects:
            try:
                nodes_response = requests.get(
                    f"{self.config['gns3_host']}/v2/projects/{project['project_id']}/nodes",
                    auth=self._auth(), timeout=5,
                )
                if nodes_response.status_code != 200:
                    complete = False
                    continue
                for node in nodes_response.json():
                    for image_name in images_by_template_id.get(node.get("template_id"), set()):
                        counts[image_name] += 1
            except Exception:
                complete = False
        return counts, complete

    def add_image(self):
        initial_dir = self.config.get("import_dir", DEFAULT_IMPORT_DIR)
        DISPLAY_EXTENSIONS = IMAGE_EXTENSIONS | {'.zip'}
        dlg = FileSelectorDialog(self.root, initial_dir=initial_dir,
                                title="Выберите образ или ZIP-архив",
                                extensions=DISPLAY_EXTENSIONS)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        filepath = dlg.result
        src = Path(filepath)
        if not src.is_file():
            ErrorDialog(self.root, "Ошибка", "Файл не существует.")
            return

        # Если файл является ZIP-архивом — распаковываем
        if src.suffix.lower() == '.zip':
            try:
                extracted = extract_images_from_zip(src, TARGET_DIR)
            except Exception as e:
                ErrorDialog(self.root, "Ошибка распаковки ZIP", str(e))
                return
            if not extracted:
                ErrorDialog(self.root, "Ошибка", "В ZIP-архиве не найдено файлов образов с допустимыми расширениями.")
                return
            self.refresh_list()
            self.status_var.set(f"Распаковано {len(extracted)} файлов из {src.name}")
            # Предлагаем создать шаблон для первого извлечённого файла
            if messagebox.askyesno("Шаблон GNS3", "Создать шаблон QEMU для первого распакованного образа?"):
                self.create_template_for_image(str(extracted[0]))
            return

        # Обычный файл образа
        if src.suffix.lower() not in IMAGE_EXTENSIONS:
            ErrorDialog(self.root, "Ошибка", f"Расширение '{src.suffix}' не поддерживается как образ.")
            return

        dest = TARGET_DIR / src.name
        if dest.exists():
            if not messagebox.askyesno("Файл существует", f"'{src.name}' уже есть. Заменить?"):
                return
        try:
            shutil.copy2(src, dest)
            self.refresh_list()
            self.status_var.set(f"Добавлен: {src.name}")
        except Exception as e:
            ErrorDialog(self.root, "Ошибка копирования", str(e))
            return
        if messagebox.askyesno("Шаблон GNS3", "Создать шаблон QEMU?"):
            self.create_template_for_image(str(dest))

    def create_template_for_image(self, image_path):
        try:
            dialog = CreateTemplateDialog(self.root, image_path, self.config,
                                          on_submit=self._create_template_from_params)
        except Exception:
            log_event(f"ОШИБКА при открытии окна создания:\n{traceback.format_exc()}")
            ErrorDialog(self.root, "Ошибка", traceback.format_exc())
            return
        try:
            if dialog.winfo_exists():
                self.root.wait_window(dialog)
        except tk.TclError as e:
            log_event(f"Ожидание закрытия окна прервано: {e}")

    def _create_template_from_params(self, td):
        """Отправляет параметры шаблона в GNS3 и показывает результат."""
        url = f"{self.config['gns3_host']}/v2/templates"
        payload = {
            "name": td["name"], "template_type": "qemu", "compute_id": "local",
            "qemu_path": td["qemu_path"], "ram": td["ram"], "cpus": td["cpus"],
            "hda_disk_image": td["hda_disk_image"], "hdb_disk_image": td["hdb_disk_image"],
            "cdrom_image": td["cdrom_image"], "boot_priority": td["boot_priority"],
            "platform": td["platform"], "console_type": td["console_type"],
            "adapters": td["adapters"], "adapter_type": td["adapter_type"],
            "mac_address": td["mac_address"] if td["mac_address"] else None,
            "options": td["options"], "linked_clone": td["linked_clone"],
            "symbol": td.get("symbol", ""), "category": td.get("category", ""),
            "hda_disk_interface": td.get("hda_disk_interface", ""),
            "hdb_disk_interface": td.get("hdb_disk_interface", "")
        }
        for key in ["hdb_disk_image","cdrom_image","mac_address","options","symbol","category",
                     "hdb_disk_interface"]:
            if key in payload and not payload[key]:
                del payload[key]
        log_event(f"POST {url} payload={payload}")
        self.status_var.set(f"Создаю шаблон '{td['name']}'…")
        try:
            auth = self._auth()
            resp = requests.post(url, json=payload, auth=auth, timeout=10)
            log_event(f"Ответ GNS3: HTTP {resp.status_code} {resp.text[:300]}")
            if resp.status_code == 201:
                messagebox.showinfo("Шаблон создан", f"Шаблон '{td['name']}' создан.", parent=self.root)
                self.status_var.set(f"Шаблон создан: {td['name']}")
                self.update_api_status(True)
                self.refresh_template_associations()
            else:
                self.status_var.set(f"Ошибка создания шаблона: HTTP {resp.status_code}")
                ErrorDialog(self.root, "Ошибка API", f"Код: {resp.status_code}\n{resp.text}")
                self.update_api_status(False)
        except requests.exceptions.ConnectionError as e:
            log_event(f"Нет соединения с GNS3: {e}")
            self.status_var.set("Нет соединения с GNS3")
            ErrorDialog(self.root, "Ошибка", f"Нет соединения с GNS3 ({url})\n{e}")
            self.update_api_status(False)
        except Exception as e:
            log_event(f"Исключение при создании шаблона: {e!r}\n{traceback.format_exc()}")
            self.status_var.set(f"Ошибка создания шаблона: {e}")
            ErrorDialog(self.root, "Ошибка", traceback.format_exc())
            self.update_api_status(False)

    def _auth(self):
        if self.config["gns3_user"] and self.config["gns3_pass"]:
            return HTTPBasicAuth(self.config["gns3_user"], self.config["gns3_pass"])
        return None

    def open_config_iso_manager(self):
        ConfigISOManagerDialog(self.root, self.config)

    def delete_images(self):
        selected = self.tree.selection()
        if not selected: ErrorDialog(self.root, "Ошибка", "Ничего не выбрано"); return
        files_to_delete = []
        for item in selected:
            values = self.tree.item(item, "values")
            if values: files_to_delete.append(TARGET_DIR / values[0])
        if not files_to_delete: return
        templates_to_delete = self._find_templates_by_images(files_to_delete)
        total_nodes = 0
        for tmpl in templates_to_delete:
            nodes = self._find_nodes_by_template(tmpl["template_id"])
            total_nodes += len(nodes); tmpl["nodes"] = nodes
        dlg = ConfirmDeleteDialog(self.root, files_to_delete, templates_to_delete, total_nodes)
        self.root.wait_window(dlg)
        if not dlg.result: return
        errors = []
        if templates_to_delete:
            for tmpl in templates_to_delete:
                for node in tmpl.get("nodes", []):
                    try: self._delete_node(node["project_id"], node["node_id"])
                    except Exception as e: errors.append(f"Узел {node['name']}: {e}")
                try: self._delete_template(tmpl["template_id"])
                except Exception as e: errors.append(f"Шаблон {tmpl['name']}: {e}")
        try:
            from send2trash import send2trash
            use_trash = True
        except ImportError:
            use_trash = False
        for f in files_to_delete:
            try:
                if use_trash: send2trash(str(f))
                else: os.remove(f)
            except Exception as e: errors.append(f"Файл {f.name}: {e}")
        self.refresh_list()
        if errors: ErrorDialog(self.root, "Ошибки", "\n".join(errors)); self.status_var.set("Удаление с ошибками")
        else: self.status_var.set(f"Удалено файлов: {len(files_to_delete)}, шаблонов: {len(templates_to_delete)}, узлов: {total_nodes}")

    def delete_unused_images(self):
        used_filenames = self._get_used_image_filenames()
        if used_filenames is None: ErrorDialog(self.root, "Ошибка", "Не удалось получить список шаблонов."); return
        if not TARGET_DIR.exists(): ErrorDialog(self.root, "Ошибка", "Папка образов не существует."); return
        unused_files = []
        for entry in TARGET_DIR.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS and entry.name not in used_filenames:
                unused_files.append(entry)
        if not unused_files: messagebox.showinfo("Информация", "Все образы используются."); return
        dlg = ConfirmDeleteDialog(self.root, unused_files, [], 0)
        self.root.wait_window(dlg)
        if not dlg.result: return
        try:
            from send2trash import send2trash; use_trash = True
        except ImportError:
            use_trash = False
        errors = []; deleted = 0
        for f in unused_files:
            try:
                if use_trash: send2trash(str(f))
                else: os.remove(f)
                deleted += 1
            except Exception as e: errors.append(f"{f.name}: {e}")
        self.refresh_list()
        if errors: ErrorDialog(self.root, "Ошибки", "\n".join(errors)); self.status_var.set(f"Удалено {deleted}, ошибки: {len(errors)}")
        else: self.status_var.set(f"Удалено неиспользуемых: {deleted}")

    def delete_templates_with_missing_images(self):
        try:
            auth = self._auth()
            resp = requests.get(f"{self.config['gns3_host']}/v2/templates", auth=auth, timeout=10)
            if resp.status_code != 200: ErrorDialog(self.root, "Ошибка", f"Код: {resp.status_code}"); return
            all_templates = resp.json()
        except Exception as e: ErrorDialog(self.root, "Ошибка", str(e)); return
        bad_templates = []
        for tmpl in all_templates:
            disks = [tmpl.get("hda_disk_image"), tmpl.get("hdb_disk_image"), tmpl.get("cdrom_image")]
            missing = False
            for disk in disks:
                if not disk: continue
                p = Path(disk)
                if not p.is_absolute(): p = TARGET_DIR / p
                if not p.exists(): missing = True; break
            if missing: bad_templates.append({"template_id": tmpl["template_id"], "name": tmpl["name"]})
        if not bad_templates: messagebox.showinfo("Информация", "Все шаблоны в порядке."); return
        total_nodes = 0
        for bt in bad_templates:
            nodes = self._find_nodes_by_template(bt["template_id"])
            total_nodes += len(nodes); bt["nodes"] = nodes
        dlg = ConfirmDeleteDialog(self.root, [], bad_templates, total_nodes)
        self.root.wait_window(dlg)
        if not dlg.result: return
        errors = []; del_tmpl = 0; del_nodes = 0
        for bt in bad_templates:
            for node in bt.get("nodes", []):
                try: self._delete_node(node["project_id"], node["node_id"]); del_nodes += 1
                except Exception as e: errors.append(f"Узел {node['name']}: {e}")
            try: self._delete_template(bt["template_id"]); del_tmpl += 1
            except Exception as e: errors.append(f"Шаблон {bt['name']}: {e}")
        self.refresh_list()
        if errors: ErrorDialog(self.root, "Ошибки", "\n".join(errors)); self.status_var.set("Удаление с ошибками")
        else: self.status_var.set(f"Удалено шаблонов: {del_tmpl}, узлов: {del_nodes}")

    def _get_used_image_filenames(self):
        try:
            auth = self._auth()
            resp = requests.get(f"{self.config['gns3_host']}/v2/templates", auth=auth, timeout=10)
            if resp.status_code != 200: return None
            templates = resp.json()
        except: return None
        used = set()
        for t in templates:
            for key in ["hda_disk_image","hdb_disk_image","cdrom_image"]:
                path = t.get(key, "")
                if path: used.add(Path(path).name)
        return used

    def _find_templates_by_images(self, image_paths):
        try:
            auth = self._auth()
            resp = requests.get(f"{self.config['gns3_host']}/v2/templates", auth=auth, timeout=10)
            if resp.status_code != 200: return []
            templates = resp.json()
        except: return []
        target_names = {p.name for p in image_paths}
        matched = []
        for t in templates:
            hda = t.get("hda_disk_image", "")
            if hda and Path(hda).name in target_names:
                matched.append({"template_id": t["template_id"], "name": t["name"]})
        return matched

    def _find_nodes_by_template(self, template_id):
        found = []
        auth = self._auth()
        try:
            resp = requests.get(f"{self.config['gns3_host']}/v2/projects", auth=auth, timeout=10)
            if resp.status_code != 200: return found
            projects = resp.json()
        except: return found
        for proj in projects:
            pid = proj["project_id"]
            close_later = False
            if proj.get("status") != "opened":
                try: requests.post(f"{self.config['gns3_host']}/v2/projects/{pid}/open", auth=auth, timeout=5); close_later = True
                except: continue
            try:
                nodes_resp = requests.get(f"{self.config['gns3_host']}/v2/projects/{pid}/nodes", auth=auth, timeout=10)
                if nodes_resp.status_code != 200: continue
                nodes = nodes_resp.json()
            except: continue
            finally:
                if close_later:
                    try: requests.post(f"{self.config['gns3_host']}/v2/projects/{pid}/close", auth=auth, timeout=5)
                    except: pass
            for node in nodes:
                if node.get("template_id") == template_id:
                    found.append({"project_id": pid, "node_id": node["node_id"], "name": node.get("name","")})
        return found

    def _delete_node(self, pid, nid):
        auth = self._auth()
        try: requests.post(f"{self.config['gns3_host']}/v2/projects/{pid}/nodes/{nid}/stop", auth=auth, timeout=5)
        except: pass
        resp = requests.delete(f"{self.config['gns3_host']}/v2/projects/{pid}/nodes/{nid}", auth=auth, timeout=10)
        if resp.status_code not in (200, 204): raise Exception(f"HTTP {resp.status_code}")

    def _delete_template(self, tid):
        auth = self._auth()
        resp = requests.delete(f"{self.config['gns3_host']}/v2/templates/{tid}", auth=auth, timeout=10)
        if resp.status_code not in (200, 204): raise Exception(f"HTTP {resp.status_code}")

    def check_api(self):
        ok, msg = self._test_api()
        if ok:
            self.refresh_template_associations()
            messagebox.showinfo("Успех", msg)
        else: ErrorDialog(self.root, "Ошибка", msg)

    def check_api_silent(self):
        ok, _ = self._test_api()
        self.update_api_status(ok)
        if ok:
            self.refresh_template_associations()
        else:
            self.templates_by_image = None
            self.template_association_state = "Недоступно"
            self.vm_count_by_image = None
            self.vm_count_complete = False
            self.refresh_list()

    def _test_api(self):
        auth = self._auth()
        try:
            resp = requests.get(f"{self.config['gns3_host']}/v2/version", auth=auth, timeout=5)
            if resp.status_code == 200: return True, f"Версия: {resp.json().get('version','')}"
            else: return False, f"Код: {resp.status_code}"
        except Exception as e: return False, str(e)

    def update_api_status(self, ok):
        self.api_status_var.set("●")
        self.api_status_label.config(foreground="green" if ok else "red")

    def open_settings(self):
        SettingsDialog(self.root, self.config, self.on_settings_updated)

    def on_settings_updated(self):
        self.config = load_config(); self.status_var.set("Настройки сохранены."); self.check_api_silent()

def set_wm_class(window, instance="gns3imagemanager", wm_class="GNS3ImageManager"):
    """Задаёт WM_CLASS окна ровно как нужно (чтобы ярлык находил приложение).

    Tk приводит переданный className к виду «Gns3imagemanager» (первая заглавная,
    остальные строчные), поэтому ярлык с `StartupWMClass=GNS3ImageManager` окно не
    находил. Здесь WM_CLASS выставляется напрямую через Xlib: окно на миг скрывается
    (чтобы оконный менеджер прочитал класс уже при отображении), затем показывается.
    Ошибки X игнорируются, а если libX11 недоступна — остаётся вариант от Tk,
    приложение продолжает работать.
    """
    try:
        import ctypes

        x11 = ctypes.CDLL("libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.POINTER(ctypes.c_ulong),
                                   ctypes.POINTER(ctypes.c_ulong),
                                   ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                                   ctypes.POINTER(ctypes.c_uint)]
        x11.XQueryTree.restype = ctypes.c_int
        x11.XSetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        x11.XSetClassHint.restype = ctypes.c_int
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        # Свой обработчик ошибок X здесь ставить нельзя: XSetErrorHandler глобальный
        # и перетирает обработчик Tk (проверено — приложение падало).
        window.withdraw()                 # окно создаётся, но ещё не отображается
        window.update_idletasks()
        display = x11.XOpenDisplay(None)
        if not display:
            return False
        try:
            inner = window.winfo_id()
            root_ret = ctypes.c_ulong()
            parent_ret = ctypes.c_ulong()
            children = ctypes.POINTER(ctypes.c_ulong)()
            n_children = ctypes.c_uint()
            # WM читает WM_CLASS у окна верхнего уровня — у Tk это окно-обёртка,
            # родитель внутреннего окна.
            ok = x11.XQueryTree(ctypes.c_void_p(display), ctypes.c_ulong(inner),
                                ctypes.byref(root_ret), ctypes.byref(parent_ret),
                                ctypes.byref(children), ctypes.byref(n_children))
            if children:
                x11.XFree(ctypes.cast(children, ctypes.c_void_p))
            target = parent_ret.value or inner
            if not ok or not target:
                return False

            class XClassHint(ctypes.Structure):
                _fields_ = [("res_name", ctypes.c_char_p), ("res_class", ctypes.c_char_p)]

            hint = XClassHint(instance.encode(), wm_class.encode())
            x11.XSetClassHint(ctypes.c_void_p(display), ctypes.c_ulong(target),
                              ctypes.byref(hint))
            x11.XFlush(ctypes.c_void_p(display))
        finally:
            x11.XCloseDisplay(ctypes.c_void_p(display))
        window.deiconify()
        window.update_idletasks()
        log_event(f"WM_CLASS установлен: {instance} / {wm_class}")
        return True
    except Exception as e:
        log_event(f"Не удалось установить WM_CLASS через Xlib ({e!r}), остаётся вариант Tk")
        try:
            window.deiconify()
        except tk.TclError:
            pass
        return False


def main():
    # Путь к AppImage (переменную задаёт сам AppImage) — сразу видно, какая сборка запущена.
    appimage = os.environ.get("APPIMAGE") or sys.argv[0]
    log_event(f"=== Запуск GNS3 Image Manager: {appimage} | frozen={getattr(sys, 'frozen', False)} | Python {sys.version.split()[0]} ===")
    # baseName/className задают WM_CLASS окна: по нему оконный менеджер и ярлык
    # (.desktop с StartupWMClass=GNS3ImageManager) сопоставляют окно с приложением.
    root = tk.Tk(baseName="gns3imagemanager", className="GNS3ImageManager")
    set_wm_class(root)

    def _log_callback_exception(exc, val, tb):
        """Все необработанные исключения Tk пишем в журнал: в AppImage stderr не виден."""
        log_event("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:\n" + "".join(traceback.format_exception(exc, val, tb)))

    root.report_callback_exception = _log_callback_exception
    try:
        if getattr(sys, 'frozen', False):
            icon_path = os.path.join(sys._MEIPASS, "icon.png")
        else:
            icon_path = "icon.png"
        if os.path.exists(icon_path):
            icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, icon)
    except Exception:
        pass
    set_dark_theme(root)
    app = GNS3ImageManager(root)
    root.mainloop()

if __name__ == "__main__":
    main()
