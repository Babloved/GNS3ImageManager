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
- загрузка настроек из существующего ISO при создании нового (исправлено)

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
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from datetime import datetime
from io import BytesIO

import requests
from requests.auth import HTTPBasicAuth

# -------------------- Конфигурация --------------------
TARGET_DIR = Path.home() / "GNS3/images/QEMU"
IMAGE_EXTENSIONS = {".qcow2", ".img", ".vmdk", ".qcow", ".raw", ".vdi", ".vhd", ".vhdx"}
CONFIG_FILE = Path.home() / ".gns3_image_manager_config.json"
CONFIG_ISO_DIR = TARGET_DIR / "config_isos"            # папка для конфигурационных ISO
DEFAULT_IMPORT_DIR = str(Path.home() / "Downloads") if (Path.home() / "Downloads").exists() else str(Path.home())

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

# -------------------- Тёмная тема --------------------
def set_dark_theme(root):
    style = ttk.Style()
    root.tk_setPalette(background='#2e2e2e', foreground='#ffffff',
                       activeBackground='#3e3e3e', activeForeground='#ffffff')
    style.theme_use('clam')
    style.configure(".", background='#2e2e2e', foreground='#ffffff',
                    fieldbackground='#3e3e3e', troughcolor='#2e2e2e',
                    selectbackground='#4a6984', selectforeground='#ffffff')
    style.configure("Treeview", background="#2e2e2e", foreground="#ffffff",
                    fieldbackground="#2e2e2e", rowheight=25)
    style.configure("Treeview.Heading", background="#3e3e3e", foreground="#ffffff")
    style.map("Treeview", background=[('selected', '#4a6984')])
    style.configure("TLabel", background='#2e2e2e', foreground='#ffffff')
    style.configure("TButton", background='#3e3e3e', foreground='#ffffff',
                    borderwidth=1, focusthickness=3)
    style.map("TButton", background=[('active', '#4a6984')])
    style.configure("TEntry", foreground='#ffffff', fieldbackground='#3e3e3e')
    style.configure("TCombobox", foreground='#ffffff', fieldbackground='#3e3e3e',
                    background='#3e3e3e', arrowcolor='#ffffff')
    root.option_add("*TCombobox*Listbox*Background", '#3e3e3e')
    root.option_add("*TCombobox*Listbox*Foreground", '#ffffff')
    style.configure("TFrame", background='#2e2e2e')
    style.configure("TScrollbar", background='#3e3e3e', troughcolor='#2e2e2e')
    style.configure("TLabelframe", background='#2e2e2e', foreground='#ffffff')
    style.configure("TLabelframe.Label", background='#2e2e2e', foreground='#ffffff')
    style.configure("TNotebook", background='#2e2e2e', borderwidth=0)
    style.configure("TNotebook.Tab", background='#3e3e3e', foreground='#ffffff')
    style.map("TNotebook.Tab", background=[('selected', '#4a6984')])
    root.option_add("*Text.Background", '#3e3e3e')
    root.option_add("*Text.Foreground", '#ffffff')
    root.option_add("*Text.InsertBackground", '#ffffff')

# -------------------- Умное имя шаблона --------------------
def generate_template_name(image_path: str) -> str:
    name = Path(image_path).stem
    if name.lower().startswith("ngfw-"):
        parts = name.split("-")
        if len(parts) >= 4 and parts[1].lower() == "x86" and parts[2] == "64":
            version = parts[3]
            if version.lower().endswith("r") or "release" in name.lower():
                match = re.match(r"(\d+\.\d+\.\d+\.\d+[A-Za-z]?)", version)
                if match:
                    version = match.group(1)
            return f"NGFW {version}"
    clean = re.sub(r'[-_]+', ' ', name)
    return ' '.join(word.capitalize() for word in clean.split())

# -------------------- Функция максимизации окна (кроссплатформенная) --------------------
def maximize_window(window):
    """Максимизирует окно на весь экран с сохранением рамок (не fullscreen)."""
    try:
        window.state('zoomed')                     # Windows
    except tk.TclError:
        try:
            window.attributes('-zoomed', True)     # Linux (X11)
        except tk.TclError:
            window.update_idletasks()
            screen_width = window.winfo_screenwidth()
            screen_height = window.winfo_screenheight()
            window.geometry(f"{screen_width}x{screen_height}+0+0")

# -------------------- Диалог для отображения ошибок с возможностью копирования --------------------
class ErrorDialog(tk.Toplevel):
    def __init__(self, parent, title, message):
        super().__init__(parent)
        self.title(title)
        self.geometry("700x400")
        self.minsize(500, 250)
        self.resizable(True, True)

        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text=title, font=('', 10, 'bold')).pack(anchor=tk.W, pady=(0,5))

        text_frame = ttk.Frame(main_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(text_frame, wrap=tk.WORD, bg="#3e3e3e", fg="white", insertbackground="white")
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
        self.text.focus_set()
        self.text.bind("<Control-c>", lambda e: self._copy_to_clipboard())
        maximize_window(self)

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

# -------------------- Кастомный диалог выбора файла --------------------
class FileSelectorDialog(tk.Toplevel):
    def __init__(self, parent, initial_dir, title="Выберите файл", extensions=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("900x650")
        self.minsize(700, 500)
        self.result = None
        self.current_dir = Path(initial_dir) if initial_dir and Path(initial_dir).exists() else Path.home()
        self.extensions = set(extensions) if extensions is not None else IMAGE_EXTENSIONS

        nav_frame = ttk.Frame(self)
        nav_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(nav_frame, text="Вверх", command=self._go_up).pack(side=tk.LEFT, padx=5)
        self.dir_var = tk.StringVar(value=str(self.current_dir))
        ttk.Entry(nav_frame, textvariable=self.dir_var, width=60).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(nav_frame, text="Перейти", command=self._browse_to).pack(side=tk.LEFT, padx=5)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("name", "size", "type")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode=tk.BROWSE)
        self.tree.heading("name", text="Имя")
        self.tree.heading("size", text="Размер")
        self.tree.heading("type", text="Тип")
        self.tree.column("name", width=450)
        self.tree.column("size", width=120, anchor=tk.CENTER)
        self.tree.column("type", width=100, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._on_double_click)

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
                dirs.append((entry.name, "", "<Каталог>", entry))
            else:
                if entry.suffix.lower() in self.extensions:
                    size = entry.stat().st_size
                    size_str = f"{size/1024/1024:.1f} МБ" if size > 1024*1024 else f"{size/1024:.1f} КБ"
                    files.append((entry.name, size_str, entry.suffix.upper(), entry))
        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())
        for item in dirs + files:
            self.tree.insert("", tk.END, values=item[:3], tags=("dir" if item[2]=="<Каталог>" else "file",))

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

        canvas = tk.Canvas(self, bg='#2e2e2e', highlightthickness=0)
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
        ttk.Checkbutton(main_frame, variable=self.linked_clone_var).grid(row=row, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(main_frame, text="Linked base по умолчанию:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
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
    def __init__(self, parent, config, iso_type=None):
        super().__init__(parent)
        self.config = config
        self.result_path = None
        self.title("Создание конфигурационного ISO")
        self.geometry("800x650")
        self.minsize(600, 450)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        common_frame = ttk.Frame(notebook, padding=10)
        notebook.add(common_frame, text="Общие")
        row = 0
        ttk.Label(common_frame, text="Тип устройства:").grid(row=row, column=0, sticky="w", padx=5, pady=5)
        self.types = [("NGFW nocloud", "ngfw"), ("Cisco config", "cisco"), ("MikroTik script", "mikrotik")]
        self.type_var = tk.StringVar(value=self.types[0][0])
        ttk.Combobox(common_frame, textvariable=self.type_var, values=[t[0] for t in self.types], state="readonly", width=20).grid(row=row, column=1, sticky="w", padx=5, pady=5)
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
        # Кнопка загрузки из ISO
        load_btn = ttk.Button(common_frame, text="Загрузить из ISO...", command=self._load_from_iso)
        load_btn.grid(row=row, column=0, columnspan=2, pady=10)

        self.content_notebook = ttk.Notebook(notebook)
        notebook.add(self.content_notebook, text="Содержимое")

        self.user_data_text = None
        self.meta_data_text = None
        self.vendor_data_text = None
        self.config_text = None
        self.config_file_name_var = tk.StringVar(value="startup-config.cfg")

        self._build_content_tabs()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(0, 10))
        ttk.Button(btn_frame, text="Создать ISO", command=self.create_iso).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=10)

        maximize_window(self)

    def _build_content_tabs(self):
        for child in self.content_notebook.winfo_children():
            child.destroy()
        if self.type_var.get().startswith("NGFW"):
            tab_user = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_user, text="user-data")
            lbl = ttk.Label(tab_user, text="user-data (YAML):", font=("", 9, "bold"))
            lbl.pack(anchor="w")
            self.user_data_text = tk.Text(tab_user, wrap="word", bg="#3e3e3e", fg="white", insertbackground="white")
            self.user_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.user_data_text.insert("1.0", "#cloud-config\npassword: admin\nchpasswd: { expire: False }\nssh_pwauth: true\n")

            tab_meta = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_meta, text="meta-data")
            lbl2 = ttk.Label(tab_meta, text="meta-data:", font=("", 9, "bold"))
            lbl2.pack(anchor="w")
            self.meta_data_text = tk.Text(tab_meta, wrap="word", bg="#3e3e3e", fg="white", insertbackground="white", height=5)
            self.meta_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.meta_data_text.insert("1.0", "instance-id: i-ngfw\nlocal-hostname: ngfw\n")

            tab_vendor = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_vendor, text="vendor-data")
            lbl3 = ttk.Label(tab_vendor, text="vendor-data (опционально):", font=("", 9, "bold"))
            lbl3.pack(anchor="w")
            self.vendor_data_text = tk.Text(tab_vendor, wrap="word", bg="#3e3e3e", fg="white", insertbackground="white")
            self.vendor_data_text.pack(fill=tk.BOTH, expand=True, pady=5)
            self.vendor_data_text.insert("1.0", "")
        else:
            tab_file = ttk.Frame(self.content_notebook, padding=10)
            self.content_notebook.add(tab_file, text="Конфигурационный файл")
            frame_name = ttk.Frame(tab_file)
            frame_name.pack(fill=tk.X, pady=5)
            ttk.Label(frame_name, text="Имя файла внутри ISO:").pack(side=tk.LEFT)
            ttk.Entry(frame_name, textvariable=self.config_file_name_var, width=30).pack(side=tk.LEFT, padx=5)
            self.config_text = tk.Text(tab_file, wrap="word", bg="#3e3e3e", fg="white", insertbackground="white")
            self.config_text.pack(fill=tk.BOTH, expand=True, pady=5)
            if self.type_var.get().startswith("Cisco"):
                self.config_text.insert("1.0", "!\nhostname Router\n!\n")
            else:
                self.config_text.insert("1.0", "/system identity set name=MyRouter\n")

    def _switch_type(self):
        self._build_content_tabs()

    def _load_from_iso(self):
        """Загружает содержимое из существующего ISO и заполняет поля редактора."""
        dlg = FileSelectorDialog(self, initial_dir=str(CONFIG_ISO_DIR), title="Выберите ISO для загрузки", extensions=['.iso'])
        self.wait_window(dlg)
        if not dlg.result:
            return
        iso_path = dlg.result
        try:
            content_dict = read_iso_content(iso_path)
        except Exception as e:
            ErrorDialog(self, "Ошибка чтения ISO", str(e))
            return

        # Проверяем, является ли ISO nocloud
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
            # Generic ISO: выбираем первый файл
            files = [f for f in content_dict.keys() if f not in ('.', '..')]
            if not files:
                ErrorDialog(self, "Ошибка", "В ISO не найдено файлов с данными.")
                return
            chosen = files[0]
            data = content_dict[chosen]
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

        messagebox.showinfo("Готово", f"Данные загружены из {Path(iso_path).name}")
        self.deiconify()      # если окно было свернуто, разворачиваем
        self.lift()           # поднимаем на передний план
        self.focus_force()    # принудительно устанавливаем фокус

    def create_iso(self):
        iso_name = self.iso_name_var.get().strip()
        if not iso_name:
            ErrorDialog(self, "Ошибка", "Введите имя ISO-файла.")
            return
        if not iso_name.endswith(".iso"):
            iso_name += ".iso"
        out_dir = Path(self.dir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / iso_name

        # Проверка наличия mkisofs
        if MKISOFS_CMD is None:
            ErrorDialog(self, "Ошибка", "Утилита mkisofs/genisoimage не установлена.\nУстановите её (apt install genisoimage или аналогичное).")
            return

        try:
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
        self.geometry("700x500")
        self.minsize(500, 350)

        top_frame = ttk.Frame(self, padding=5)
        top_frame.pack(fill=tk.X)
        ttk.Button(top_frame, text="Создать новый ISO", command=self._create_iso).pack(side=tk.LEFT, padx=5)
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

    def _create_iso(self):
        dlg = ConfigISOEditorDialog(self, self.config)
        self.wait_window(dlg)
        if dlg.result_path:
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
    def __init__(self, parent, image_path, config):
        super().__init__(parent)
        self.image_path = image_path
        self.config = config
        self.result = None
        self.title("Создать шаблон QEMU в GNS3")
        self.geometry("700x600")
        self.resizable(True, True)
        self.minsize(600, 500)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tab_basic = ttk.Frame(notebook); notebook.add(tab_basic, text="Основные"); self._create_basic_tab(tab_basic)
        tab_disks = ttk.Frame(notebook); notebook.add(tab_disks, text="Диски"); self._create_disks_tab(tab_disks)
        tab_net = ttk.Frame(notebook); notebook.add(tab_net, text="Сеть"); self._create_network_tab(tab_net)
        tab_adv = ttk.Frame(notebook); notebook.add(tab_adv, text="Дополнительно"); self._create_advanced_tab(tab_adv)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(0, 10))
        ttk.Button(btn_frame, text="Создать", command=self.create).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.LEFT, padx=5)
        self.hda_var.set(image_path)

        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        maximize_window(self)

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
        ttk.Combobox(frame, textvariable=self.category_var, values=["router", "switch", "guest", "firewall"], width=20).grid(row=r, column=1, sticky="w", padx=5, pady=5)
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

        ttk.Label(frame, text="CDROM образ:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        self.cdrom_var = tk.StringVar()
        cdrom_frame = ttk.Frame(frame)
        cdrom_frame.grid(row=r, column=1, columnspan=3, sticky="ew", pady=5)
        ttk.Entry(cdrom_frame, textvariable=self.cdrom_var, width=30).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cdrom_frame, text="Обзор", command=self._browse_cdrom).pack(side=tk.LEFT, padx=5)
        ttk.Button(cdrom_frame, text="Создать ISO", command=self._create_config_iso).pack(side=tk.LEFT, padx=5)
        r += 1

        ttk.Label(frame, text="* Пути могут быть абсолютными или относительными (от images/QEMU)").grid(row=r, column=0, columnspan=4, sticky="w", padx=5, pady=10)
        frame.columnconfigure(1, weight=1)

    def _browse_file(self, stringvar):
        initial = self.config.get("import_dir", DEFAULT_IMPORT_DIR)
        dlg = FileSelectorDialog(self, initial_dir=initial, title="Выберите файл")
        self.wait_window(dlg)
        if dlg.result:
            stringvar.set(dlg.result)

    def _browse_cdrom(self):
        dlg = FileSelectorDialog(self, initial_dir=str(CONFIG_ISO_DIR), title="Выберите ISO-образ", extensions=['.iso'])
        self.wait_window(dlg)
        if dlg.result:
            self.cdrom_var.set(dlg.result)

    def _create_config_iso(self):
        dlg = ConfigISOEditorDialog(self, self.config)
        self.wait_window(dlg)
        if dlg.result_path:
            self.cdrom_var.set(dlg.result_path)

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
        text = tk.Text(frame, width=40, height=6, bg='#3e3e3e', fg='#ffffff', insertbackground='#ffffff')
        text.grid(row=r, column=1, padx=5, pady=5, sticky="ew"); text.insert("1.0", self.config.get("options", ""))
        self.options_text = text; r += 1
        self.linked_clone_var = tk.BooleanVar(value=self.config.get("linked_clone", True))
        ttk.Checkbutton(frame, variable=self.linked_clone_var).grid(row=r, column=1, sticky="w")
        ttk.Label(frame, text="Linked base:").grid(row=r, column=0, sticky="w", padx=5, pady=5)
        r += 1
        ttk.Label(frame, text="(позволяет запускать несколько копий одного образа)", foreground="gray").grid(row=r, column=1, sticky="w", padx=5, pady=0)

    def create(self):
        if not self.name_var.get().strip(): ErrorDialog(self, "Ошибка", "Введите имя шаблона."); return
        if not self.hda_var.get().strip(): ErrorDialog(self, "Ошибка", "Укажите HDA диск."); return
        if not self.qemu_var.get().strip(): ErrorDialog(self, "Ошибка", "Укажите путь к QEMU."); return
        self.result = {
            "name": self.name_var.get().strip(), "ram": self.ram_var.get(), "cpus": self.cpu_var.get(),
            "qemu_path": self.qemu_var.get().strip(), "hda_disk_image": self.hda_var.get().strip(),
            "hdb_disk_image": self.hdb_var.get().strip(), "cdrom_image": self.cdrom_var.get().strip(),
            "boot_priority": self.boot_var.get(), "platform": self.platform_var.get(),
            "console_type": self.console_var.get(), "adapters": self.adapters_var.get(),
            "adapter_type": self.adapter_type_var.get(), "mac_address": self.mac_var.get().strip(),
            "options": self.options_text.get("1.0", tk.END).strip(), "linked_clone": self.linked_clone_var.get(),
            "symbol": self.symbol_var.get().strip(), "category": self.category_var.get().strip(),
            "hda_disk_interface": self.hda_iface_var.get(),
            "hdb_disk_interface": self.hdb_iface_var.get() if self.hdb_var.get().strip() else ""
        }
        self.destroy()

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
        main_frame = ttk.Frame(root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))
        row1 = ttk.Frame(top_frame); row1.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(row1, text="Обновить", command=self.refresh_list).pack(side=tk.LEFT, padx=3)
        ttk.Button(row1, text="Добавить образ...", command=self.add_image).pack(side=tk.LEFT, padx=3)
        ttk.Button(row1, text="Удалить выбранные", command=self.delete_images).pack(side=tk.LEFT, padx=3)
        ttk.Button(row1, text="Удалить неиспользуемые", command=self.delete_unused_images).pack(side=tk.LEFT, padx=3)
        ttk.Button(row1, text="Удалить шаблоны без образа", command=self.delete_templates_with_missing_images).pack(side=tk.LEFT, padx=3)
        ttk.Button(row1, text="Конфиг. ISO", command=self.open_config_iso_manager).pack(side=tk.LEFT, padx=10)
        row2 = ttk.Frame(top_frame); row2.pack(fill=tk.X)
        ttk.Button(row2, text="Проверить API", command=self.check_api).pack(side=tk.LEFT, padx=3)
        ttk.Button(row2, text="Настройки GNS3", command=self.open_settings).pack(side=tk.LEFT, padx=3)
        self.api_status_var = tk.StringVar(value="●")
        self.api_status_label = ttk.Label(row2, textvariable=self.api_status_var, foreground="red", font=("", 12))
        self.api_status_label.pack(side=tk.RIGHT, padx=(20, 5))
        ttk.Label(row2, text="API:").pack(side=tk.RIGHT)

        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("name", "size", "modified")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode=tk.EXTENDED)
        self.tree.heading("name", text="Имя файла", command=lambda: self.sort_by_column("name"))
        self.tree.heading("size", text="Размер (МБ)", command=lambda: self.sort_by_column("size"))
        self.tree.heading("modified", text="Изменён", command=lambda: self.sort_by_column("modified"))
        self.tree.column("name", width=600)
        self.tree.column("size", width=120, anchor=tk.CENTER)
        self.tree.column("modified", width=160, anchor=tk.CENTER)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X)

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
        for name, size, mtime, path in files:
            self.tree.insert("", tk.END, values=(name, size, mtime), tags=(str(path),))
        self.status_var.set(f"Образов в папке: {len(files)}")

    def add_image(self):
        initial_dir = self.config.get("import_dir", DEFAULT_IMPORT_DIR)
        dlg = FileSelectorDialog(self.root, initial_dir=initial_dir, title="Выберите образ для добавления")
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        filepath = dlg.result
        src = Path(filepath)
        if not src.is_file(): ErrorDialog(self.root, "Ошибка", "Файл не существует."); return
        dest = TARGET_DIR / src.name
        if dest.exists():
            if not messagebox.askyesno("Файл существует", f"'{src.name}' уже есть. Заменить?"): return
        try:
            shutil.copy2(src, dest); self.refresh_list(); self.status_var.set(f"Добавлен: {src.name}")
        except Exception as e: ErrorDialog(self.root, "Ошибка копирования", str(e)); return
        if messagebox.askyesno("Шаблон GNS3", "Создать шаблон QEMU?"): self.create_template_for_image(str(dest))

    def create_template_for_image(self, image_path):
        dialog = CreateTemplateDialog(self.root, image_path, self.config)
        self.root.wait_window(dialog)
        if dialog.result is None: return
        td = dialog.result
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
        try:
            auth = self._auth()
            resp = requests.post(url, json=payload, auth=auth, timeout=10)
            if resp.status_code == 201:
                messagebox.showinfo("Шаблон создан", f"Шаблон '{td['name']}' создан.")
                self.status_var.set(f"Шаблон создан: {td['name']}")
                self.update_api_status(True)
            else:
                ErrorDialog(self.root, "Ошибка API", f"Код: {resp.status_code}\n{resp.text}")
                self.update_api_status(False)
        except requests.exceptions.ConnectionError:
            ErrorDialog(self.root, "Ошибка", "Нет соединения с GNS3.")
            self.update_api_status(False)
        except Exception as e:
            ErrorDialog(self.root, "Ошибка", str(e))
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
        if ok: messagebox.showinfo("Успех", msg)
        else: ErrorDialog(self.root, "Ошибка", msg)

    def check_api_silent(self):
        ok, _ = self._test_api()
        self.update_api_status(ok)

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

def main():
    root = tk.Tk()
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