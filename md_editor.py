import sys
import os
import re
import json
import sqlite3
import base64
import requests
import webbrowser
import subprocess
import traceback
import tempfile
import zipfile
import shutil
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPlainTextEdit, QSplitter, QToolBar, QFileDialog, 
                             QMessageBox, QLabel, QMenu, QToolButton, QPushButton, 
                             QInputDialog, QLineEdit, QDialog, QFormLayout, QComboBox,
                             QSystemTrayIcon, QStyle, QProgressDialog)
from PyQt6.QtGui import QFont, QAction, QTextCursor, QIcon
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QFileSystemWatcher
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage

import markdown
import pymdownx.superfences
import pymdownx.tasklist
import pymdownx.magiclink
import pymdownx.tilde
import pymdownx.highlight

APP_VERSION = "v1.0.0"

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def parse_version(v_str):
    nums = re.findall(r'\d+', v_str)
    return [int(n) for n in nums] if nums else [0, 0, 0]

def clean_old_updates():
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        for file in os.listdir(exe_dir):
            if file.endswith('.old'):
                try:
                    os.remove(os.path.join(exe_dir, file))
                except Exception:
                    pass

class SettingsManager:
    def __init__(self):
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(self.base_dir, "settings.db")
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            defaults = [
                ('autosave_interval', '30'),
                ('gh_repo', ''), ('gh_branch', 'main'),
                ('gh_path', 'images/'), ('gh_token', ''),
                ('gh_url_format', 'raw.githubusercontent.com'),
                ('last_open_dir', '')
            ]
            for k, v in defaults:
                conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    def get_setting(self, key, default=""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
                res = cur.fetchone()
                return res[0] if res else default
        except: return default

    def set_setting(self, key, value):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

class UploadImageThread(QThread):
    finished = pyqtSignal(int, str, str)
    def __init__(self, url, headers, data, filename):
        super().__init__()
        self.url = url; self.headers = headers; self.data = data; self.filename = filename
    def run(self):
        try:
            res = requests.put(self.url, headers=self.headers, data=json.dumps(self.data))
            self.finished.emit(res.status_code, res.text, self.filename)
        except Exception as e:
            self.finished.emit(0, str(e), self.filename)

class CheckUpdateThread(QThread):
    finished = pyqtSignal(int, str)
    def __init__(self, api_url):
        super().__init__()
        self.api_url = api_url
    def run(self):
        try:
            res = requests.get(self.api_url, timeout=10)
            self.finished.emit(res.status_code, res.text)
        except Exception as e:
            self.finished.emit(0, str(e))

class DownloadUpdateThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str, str, str, str)
    
    def __init__(self, url):
        super().__init__()
        self.url = url
        self._is_cancelled = False
        
    def cancel(self):
        self._is_cancelled = True
        
    def run(self):
        original_no_proxy = os.environ.get("no_proxy")
        if "no_proxy" in os.environ:
            del os.environ["no_proxy"]

        try:
            temp_dir = tempfile.gettempdir()
            zip_path = os.path.join(temp_dir, f"md_update_{int(datetime.now().timestamp())}.zip")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            res = requests.get(self.url, stream=True, headers=headers, timeout=15)
            res.raise_for_status()
            
            total_size = int(res.headers.get('content-length', 0))
            downloaded_size = 0
            last_percent = -1 
            
            with open(zip_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=1024 * 128):
                    if self._is_cancelled:
                        f.close()
                        if os.path.exists(zip_path): os.remove(zip_path)
                        self.finished.emit(False, "已取消", "", "", "")
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded_size / total_size) * 100)
                            if percent > last_percent:
                                self.progress.emit(percent)
                                last_percent = percent
                                
            self.progress.emit(100)
            
            extract_dir = os.path.join(temp_dir, f"md_extracted_{int(datetime.now().timestamp())}")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
                
            exe_name = os.path.basename(sys.executable)
            source_dir = extract_dir
            for root, dirs, files in os.walk(extract_dir):
                if exe_name in files:
                    source_dir = root
                    break
                    
            self.finished.emit(True, "", zip_path, extract_dir, source_dir)
            
        except Exception as e:
            self.finished.emit(False, str(e), "", "", "")
        finally:
            if original_no_proxy is not None:
                os.environ["no_proxy"] = original_no_proxy

class GithubSettingsDialog(QDialog):
    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.setWindowTitle("GitHub 图床设置")
        self.initUI()

    def initUI(self):
        layout = QFormLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        layout.setSizeConstraint(QFormLayout.SizeConstraint.SetFixedSize)
        self.repo_edit = QLineEdit(self.sm.get_setting('gh_repo'))
        self.repo_edit.setPlaceholderText("例如: username/repo_name")
        self.repo_edit.setMinimumWidth(250)
        self.branch_edit = QLineEdit(self.sm.get_setting('gh_branch'))
        self.path_edit = QLineEdit(self.sm.get_setting('gh_path'))
        self.token_edit = QLineEdit(self.sm.get_setting('gh_token'))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.url_fmt = QComboBox()
        self.url_fmt.addItems(["raw.githubusercontent.com", "cdn.jsdelivr.net"])
        self.url_fmt.setCurrentText(self.sm.get_setting('gh_url_format'))
        layout.addRow("仓库地址 (User/Repo):", self.repo_edit)
        layout.addRow("分支 (Branch):", self.branch_edit)
        layout.addRow("图片目录 (Path):", self.path_edit)
        layout.addRow("个人访问令牌 (Token):", self.token_edit)
        layout.addRow("URL 格式:", self.url_fmt)
        btn_save = QPushButton("确定保存")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setStyleSheet("padding: 8px; background-color: #0366d6; color: white; border: none; border-radius: 4px;")
        btn_save.clicked.connect(self.save_and_close)
        layout.addRow(btn_save)

    def save_and_close(self):
        self.sm.set_setting('gh_repo', self.repo_edit.text())
        self.sm.set_setting('gh_branch', self.branch_edit.text())
        self.sm.set_setting('gh_path', self.path_edit.text())
        self.sm.set_setting('gh_token', self.token_edit.text())
        self.sm.set_setting('gh_url_format', self.url_fmt.currentText())
        self.accept()

class PreviewPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if is_main_frame and url.scheme() in ("http", "https"):
            webbrowser.open(url.toString())
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class MarkdownEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = SettingsManager()
        self.current_file = None
        self.upload_thread = None
        self.check_thread = None
        self.download_thread = None
        self.autosave_interval_val = 30
        self.autosave_countdown = 30
        self.pause_ui_updates = 0
        self.preview_loaded = False 
        self.file_watcher = QFileSystemWatcher()
        self.file_watcher.fileChanged.connect(self.on_external_file_change)
        
        self.initUI()
        
        self.render_timer = QTimer()
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.update_preview_content)
        
        self.autosave_timer = QTimer()
        self.autosave_timer.timeout.connect(self.auto_save_action)
        self.refresh_autosave_config()
        self.update_title("未命名 - MD 编辑器")

    def initUI(self):
        self.resize(1200, 800)
        self.icon_path = get_resource_path(os.path.join("images", "logo.png"))
        if os.path.exists(self.icon_path):
            self.setWindowIcon(QIcon(self.icon_path))
        
        main_widget = QWidget()
        main_widget.setObjectName("MainWindow")
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.create_clean_toolbar()

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("editor")
        self.editor.setFont(QFont("Consolas", 12))
        self.editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self.show_editor_context_menu)
        self.editor.textChanged.connect(self.on_text_changed)
        self.editor.verticalScrollBar().valueChanged.connect(self.sync_scroll_preview)

        self.preview = QWebEngineView()
        self.preview.setPage(PreviewPage(self.preview))
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self.show_preview_context_menu)
        
        self.splitter.addWidget(self.editor)
        self.splitter.addWidget(self.preview)
        self.splitter.setSizes([600, 600])
        main_layout.addWidget(self.splitter)

        self.setup_statusbar(main_layout)
        self.apply_modern_qss()
        self.init_preview_frame()
        self.init_system_tray()

    def update_title(self, title):
        modified_mark = "*" if self.editor.document().isModified() else ""
        self.setWindowTitle(f"{title}{modified_mark}")

    def apply_modern_qss(self):
        self.setStyleSheet("""
            #MainWindow { background-color: #ffffff; }
            QToolBar { background-color: #ffffff; border-bottom: 1px solid #e1e4e8; padding: 5px; spacing: 10px; }
            QToolButton { border: none; padding: 6px 12px; border-radius: 4px; color: #24292e; }
            QToolButton:hover { background-color: #f3f4f6; }
            QMenu { background-color: white; border: 1px solid #d1d5da; margin: 5px; }
            QMenu::item { padding: 6px 28px; background: transparent; }
            QMenu::item:selected { background-color: #0366d6; color: #ffffff; }
            #editor { border: none; padding: 25px; line-height: 1.6; }
            #footer { background-color: #f6f8fa; border-top: 1px solid #e1e4e8; }
            QPushButton#modeBtn { border: 1px solid #e1e4e8; background: #ffffff; padding: 4px 15px; border-radius: 4px; font-size: 12px; }
            QPushButton#modeBtn:checked { background-color: #0366d6; color: #ffffff; }
        """)

    def create_clean_toolbar(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addWidget(self.create_menu_btn("文件", [
            ("新建", self.new_file, "Ctrl+N"), 
            ("打开", self.open_file, "Ctrl+O"), 
            ("保存", self.save_file, "Ctrl+S")
        ]))

        toolbar.addWidget(self.create_menu_btn("编辑工具", [
            ("一级标题", lambda: self.insert_format("# ")),
            ("二级标题", lambda: self.insert_format("## ")),
            ("无序列表", lambda: self.insert_format("- ")),
            ("代码块", lambda: self.insert_format("```code\n\n```")),
            ("插入表格", lambda: self.insert_format("| 标题 | 标题 |\n| --- | --- |\n| 内容 | 内容 |")),
            ("引用文字", lambda: self.insert_format("> ")),
            ("分隔线", lambda: self.insert_format("\n---\n")),
            ("图片 (本地上传)", self.upload_local_image),
            ("图片 (在线链接)", self.import_image_url),
        ]))

        toolbar.addWidget(self.create_menu_btn("设置", [
            ("自动保存频率", self.set_autosave_interval),
            ("GitHub 图床配置", self.open_gh_settings),
            ("设为系统默认 MD 应用", self.set_default_app),
            ("检查更新", self.check_for_updates),
            ("关于", self.show_about_dialog)
        ]))

    def create_menu_btn(self, text, actions):
        btn = QToolButton()
        btn.setText(text)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(btn)
        for item in actions:
            name = item[0]
            func = item[1]
            act = QAction(name, self)
            if len(item) > 2:
                act.setShortcut(item[2])
                self.addAction(act)
            act.triggered.connect(func)
            menu.addAction(act)
        btn.setMenu(menu)
        return btn

    def init_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        if os.path.exists(self.icon_path):
            self.tray_icon.setIcon(QIcon(self.icon_path))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        
        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("完全退出", self)
        quit_action.triggered.connect(self.quit_app)
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def quit_app(self):
        if self.maybe_save():
            self.tray_icon.hide()
            QApplication.instance().quit()

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.isHidden() or self.isMinimized():
                self.showNormal()
                self.activateWindow()
            else:
                self.hide()

    def show_custom_msg(self, title, text, icon=QMessageBox.Icon.Information):
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setIcon(icon)
        msg.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        msg.exec()

    def open_file_from_external(self, file_path):
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized | Qt.WindowState.WindowActive)
        self.show()
        self.activateWindow()
        self.raise_()
        
        if not os.path.exists(file_path):
            return
            
        if self.maybe_save():
            self.open_file_by_path(file_path)

    def check_for_updates(self):
        self.save_status_label.setText(" (正在检查更新...)")
        self.pause_ui_updates = 15
        api_url = "https://api.github.com/repos/QsSama-W/MD_Editor/releases/latest"
        self.check_thread = CheckUpdateThread(api_url)
        self.check_thread.finished.connect(self.on_check_update_finished)
        self.check_thread.start()

    def on_check_update_finished(self, status_code, response_text):
        self.save_status_label.setText("")
        self.pause_ui_updates = 0
        if status_code == 200:
            try:
                data = json.loads(response_text)
                latest_version = data.get("tag_name", "")
                release_url = data.get("html_url", "https://github.com/QsSama-W/MD_Editor/releases")
                if latest_version:
                    current_v = parse_version(APP_VERSION)
                    latest_v = parse_version(latest_version)
                    if latest_v > current_v:
                        msg = QMessageBox(self)
                        msg.setWindowTitle("发现新版本")
                        msg.setText(f"当前版本: {APP_VERSION}\n最新版本: {latest_version}\n\n是否立即在后台下载并更新？")
                        msg.setIcon(QMessageBox.Icon.Information)
                        btn_go = msg.addButton("立即更新", QMessageBox.ButtonRole.AcceptRole)
                        msg.addButton("暂不更新", QMessageBox.ButtonRole.RejectRole)
                        msg.exec()
                        
                        if msg.clickedButton() == btn_go:
                            if not getattr(sys, 'frozen', False):
                                self.show_custom_msg("提示", "检测到您正在使用源码运行。覆盖更新功能仅在打包后可用，将为您打开浏览器。")
                                webbrowser.open(release_url)
                                return
                            
                            download_url = None
                            for asset in data.get("assets", []):
                                if asset.get("name", "").lower().endswith(".zip"):
                                    download_url = asset.get("browser_download_url")
                                    break
                            
                            if not download_url:
                                self.show_custom_msg("更新失败", "未在最新 Release 中找到 .zip 更新包，将为您打开浏览器手动下载。")
                                webbrowser.open(release_url)
                                return
                                
                            self.start_download_update(download_url)
                    else:
                        self.show_custom_msg("检查更新", f"当前已经是最新版本 ({APP_VERSION})。")
            except Exception as e:
                self.show_custom_msg("检查更新失败", f"解析数据错误: {str(e)}", QMessageBox.Icon.Warning)
        elif status_code == 404:
            self.show_custom_msg("检查更新", "当前仓库暂未发布任何 Release 版本。")
        else:
            self.show_custom_msg("检查更新失败", f"网络异常或无法获取更新信息:\n{response_text}", QMessageBox.Icon.Warning)

    def start_download_update(self, url):
        self.progress_dialog = QProgressDialog("正在下载更新，请稍候...", "取消", 0, 100, self)
        self.progress_dialog.setWindowTitle("软件更新中")
        self.progress_dialog.setFixedSize(260, self.progress_dialog.sizeHint().height())
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        
        self.download_thread = DownloadUpdateThread(url)
        self.download_thread.progress.connect(self.progress_dialog.setValue)
        self.download_thread.finished.connect(self.on_download_finished)
        self.progress_dialog.canceled.connect(self.download_thread.cancel)
        self.progress_dialog.show()
        self.download_thread.start()

    def on_download_finished(self, success, error_msg, zip_path, extract_dir, source_dir):
        self.progress_dialog.close()
        if success:
            msg = QMessageBox(self)
            msg.setWindowTitle("更新准备就绪")
            msg.setText("新版本已下载并解压完毕！\n请注意：点击【立即重启】后软件将自动关闭，并覆盖最新文件。\n确定要继续吗？")
            msg.setIcon(QMessageBox.Icon.Information)
            btn_restart = msg.addButton("立即重启并更新", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("稍后手动处理", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            
            if msg.clickedButton() == btn_restart:
                if self.maybe_save():
                    target_dir = os.path.dirname(sys.executable)
                    exe_path = sys.executable
                    exe_name = os.path.basename(exe_path)
                    bat_path = os.path.join(tempfile.gettempdir(), "update_md_editor.bat")
                    bat_content = f"""@echo off
chcp 65001 > NUL
echo 正在为您部署更新，请稍候 (请勿关闭此窗口)...
timeout /t 1 /nobreak > NUL
taskkill /F /IM "{exe_name}" > NUL 2>&1
timeout /t 1 /nobreak > NUL
xcopy /E /Y /C /H /R "{source_dir}\\*" "{target_dir}\\"
rmdir /S /Q "{extract_dir}"
del /F /Q "{zip_path}"
start "" "{exe_path}"
del "%~f0"
"""
                    with open(bat_path, "w", encoding="utf-8") as f:
                        f.write(bat_content)
                    subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=subprocess.CREATE_NEW_CONSOLE)
                    QApplication.instance().quit()
        else:
            if error_msg != "已取消":
                self.show_custom_msg("更新失败", f"下载或解压过程中发生错误:\n{error_msg}", QMessageBox.Icon.Critical)

    def show_about_dialog(self):
        about_html = f"""
        <div style='font-family: "Microsoft YaHei", sans-serif;'>
            <h3>MD 编辑器</h3>
            <p>一款轻量级、支持实时预览与 GitHub 图床集成的 Markdown 编辑器。</p>
            <p><b>当前版本：</b> {APP_VERSION}</p>
            <p><b>主要功能：</b></p>
            <ul>
                <li>所见即所得的双栏实时预览</li>
                <li>图片一键上传至 GitHub 图床</li>
                <li>文档防丢失自动保存机制</li>
            </ul>
            <hr>
            <p><b>开发者：</b> QsSama-W</p>
            <p><b>GitHub 主页：</b> <a href="https://github.com/QsSama-W">https://github.com/QsSama-W</a></p>
            <p><b>项目地址：</b> <a href="https://github.com/QsSama-W/MD_Editor">https://github.com/QsSama-W/MD_Editor</a></p>
        </div>
        """
        QMessageBox.about(self, "关于", about_html)

    def set_default_app(self):
        if os.name != 'nt':
            self.show_custom_msg("提示", "目前该功能仅支持 Windows 系统。", QMessageBox.Icon.Warning)
            return
        try:
            import winreg
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
                cmd = f'"{exe_path}" "%1"'
            else:
                exe_path = sys.executable
                script_path = os.path.abspath(sys.argv[0])
                cmd = f'"{exe_path}" "{script_path}" "%1"'

            key_path_ext = r"Software\Classes\.md"
            key_path_prog = r"Software\Classes\MDEditor.Document"
            key_path_cmd = r"Software\Classes\MDEditor.Document\shell\open\command"

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path_ext) as key:
                winreg.SetValue(key, "", winreg.REG_SZ, "MDEditor.Document")
            
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path_prog) as key:
                winreg.SetValue(key, "", winreg.REG_SZ, "Markdown Document")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path_cmd) as key:
                winreg.SetValue(key, "", winreg.REG_SZ, cmd)

            self.show_custom_msg("成功", "已成功将本程序设置为系统 .md 文件的默认打开应用！")
        except Exception as e:
            self.show_custom_msg("错误", f"设置失败，错误信息: {e}", QMessageBox.Icon.Critical)

    def open_gh_settings(self):
        dialog = GithubSettingsDialog(self.settings, self)
        dialog.exec()

    def upload_local_image(self):
        repo = self.settings.get_setting('gh_repo')
        token = self.settings.get_setting('gh_token')
        if not repo or not token:
            self.show_custom_msg("提示", "请先在'设置'中配置 GitHub 仓库信息和 Token！", QMessageBox.Icon.Warning)
            return
        file_path, _ = QFileDialog.getOpenFileName(self, "选择图片", "", "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        if not file_path: return
        try:
            with open(file_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.path.basename(file_path)}"
            gh_path = self.settings.get_setting('gh_path').strip("/")
            full_api_url = f"https://api.github.com/repos/{repo}/contents/{gh_path}/{filename}"
            headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
            data = {
                "message": f"Upload image via MD Editor: {filename}",
                "content": encoded_string,
                "branch": self.settings.get_setting('gh_branch')
            }
            self.save_status_label.setText(" (正在后台上传图片...)")
            self.pause_ui_updates = 60 
            self.upload_thread = UploadImageThread(full_api_url, headers, data, filename)
            self.upload_thread.finished.connect(self.on_upload_image_finished)
            self.upload_thread.start()
        except Exception as e:
            self.show_custom_msg("错误", f"读取图片文件失败: {str(e)}", QMessageBox.Icon.Critical)

    def on_upload_image_finished(self, status_code, response_text, filename):
        if status_code == 201:
            repo = self.settings.get_setting('gh_repo')
            branch = self.settings.get_setting('gh_branch')
            gh_path = self.settings.get_setting('gh_path').strip("/")
            fmt = self.settings.get_setting('gh_url_format')
            if "jsdelivr" in fmt:
                final_url = f"https://cdn.jsdelivr.net/gh/{repo}@{branch}/{gh_path}/{filename}"
            else:
                final_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{gh_path}/{filename}"
            self.insert_format(f"![{filename}]({final_url})")
            self.save_status_label.setText(" (图片上传成功！)")
            self.pause_ui_updates = 3 
            self.autosave_countdown = self.autosave_interval_val
        else:
            self.save_status_label.setText(" (上传失败)")
            self.pause_ui_updates = 3
            error_msg = response_text if status_code != 0 else "本地网络错误"
            self.show_custom_msg("上传失败", f"GitHub 返回错误或网络异常:\n{error_msg}", QMessageBox.Icon.Critical)

    def import_image_url(self):
        dialog = QInputDialog(self)
        dialog.setWindowTitle("导入图片")
        dialog.setFixedSize(260, dialog.sizeHint().height())
        dialog.setLabelText("图片链接 (http/https):")
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            url = dialog.textValue()
            if url.startswith(("http://", "https://")):
                self.insert_format(f"![图片]({url.strip()})")

    def set_autosave_interval(self):
        dialog = QInputDialog(self)
        dialog.setWindowTitle("自动保存频率")
        dialog.setFixedSize(260, dialog.sizeHint().height())
        dialog.setLabelText("秒 (1-60):")
        dialog.setIntValue(int(self.settings.get_setting('autosave_interval', '30')))
        dialog.setIntRange(1, 60)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.set_setting('autosave_interval', dialog.intValue())
            self.refresh_autosave_config()

    def refresh_autosave_config(self):
        self.autosave_interval_val = int(self.settings.get_setting('autosave_interval', '30'))
        self.autosave_countdown = self.autosave_interval_val
        self.pause_ui_updates = 0
        self.autosave_timer.start(1000)
        self.save_status_label.setText(f" (自动保存: {self.autosave_countdown}s)")

    def auto_save_action(self):
        self.autosave_countdown -= 1
        if self.pause_ui_updates > 0:
            self.pause_ui_updates -= 1
        else:
            self.save_status_label.setText(f" (自动保存: {self.autosave_countdown}s)")

        if self.autosave_countdown <= 0:
            if self.current_file and self.editor.document().isModified():
                try:
                    with open(self.current_file, 'w', encoding='utf-8') as f: 
                        f.write(self.editor.toPlainText())
                    self.editor.document().setModified(False)
                    self.save_status_label.setText(" (已自动保存)")
                    self.pause_ui_updates = 2
                    title = f"{os.path.basename(self.current_file)} - MD 编辑器"
                    self.setWindowTitle(title)
                    self.start_watching_file(self.current_file)
                except: pass
            
            self.autosave_countdown = self.autosave_interval_val
            if self.pause_ui_updates == 0:
                self.save_status_label.setText(f" (自动保存: {self.autosave_countdown}s)")

    def setup_statusbar(self, layout):
        footer = QWidget(); footer.setObjectName("footer"); footer.setFixedHeight(35)
        h_box = QHBoxLayout(footer); h_box.setContentsMargins(15, 0, 15, 0)
        self.char_label = QLabel("字符数: 0"); self.save_status_label = QLabel("")
        h_box.addWidget(self.char_label); h_box.addWidget(self.save_status_label); h_box.addStretch()
        self.btn_contrast = QPushButton("对比模式"); self.btn_preview = QPushButton("预览模式")
        for b in [self.btn_contrast, self.btn_preview]: 
            b.setObjectName("modeBtn"); b.setCheckable(True)
        self.btn_contrast.setChecked(True); self.btn_contrast.clicked.connect(self.mode_contrast); self.btn_preview.clicked.connect(self.mode_preview_only)
        h_box.addWidget(self.btn_contrast); h_box.addWidget(self.btn_preview)
        layout.addWidget(footer)

    def init_preview_frame(self):
        css_path = get_resource_path(os.path.join("css", "github.css"))
        css_content = ""
        if os.path.exists(css_path):
            try:
                with open(css_path, "r", encoding="utf-8") as f:
                    css_content = f.read()
            except: pass
            
        html = f"""<html><head><meta charset="utf-8">
            <style>
                {css_content}
                body {{ box-sizing: border-box; margin: 0 auto; padding: 45px; background-color: #fff; font-family: sans-serif;}}
                .markdown-body {{ max-width: 850px; margin: 0 auto; }}
                body.full-width .markdown-body {{ max-width: 95%; }}
                ::-webkit-scrollbar {{ width: 8px; }}
                ::-webkit-scrollbar-thumb {{ background: #d1d5da; border-radius: 4px; }}
            </style></head>
            <body id="content-body"><div id="write" class="markdown-body"></div></body></html>"""
        
        self.preview.loadFinished.connect(self.on_preview_load_finished)
        self.preview.setHtml(html)

    def on_preview_load_finished(self, ok):
        self.preview_loaded = True
        self.update_preview_content()

    def update_preview_content(self):
        if not getattr(self, 'preview_loaded', False):
            return 
            
        md_text = self.editor.toPlainText()
        raw_html = markdown.markdown(md_text, extensions=['tables', 'sane_lists', 'pymdownx.superfences', 'pymdownx.tasklist', 'pymdownx.magiclink', 'pymdownx.tilde'])
        js_code = f"document.getElementById('write').innerHTML = {json.dumps(raw_html)};"
        self.preview.page().runJavaScript(js_code)

    def sync_scroll_preview(self, value):
        sb = self.editor.verticalScrollBar()
        if sb.maximum() > 0:
            percentage = value / sb.maximum()
            js = f"window.scrollTo(0, (document.body.scrollHeight - window.innerHeight) * {percentage});"
            self.preview.page().runJavaScript(js)

    def on_text_changed(self):
        self.char_label.setText(f"字符数: {len(self.editor.toPlainText())}")
        self.render_timer.start(200)
        title = "未命名" if not self.current_file else os.path.basename(self.current_file)
        self.update_title(f"{title} - MD 编辑器")

    def mode_contrast(self):
        self.btn_contrast.setChecked(True); self.btn_preview.setChecked(False); self.editor.show()
        self.preview.page().runJavaScript("document.getElementById('content-body').classList.remove('full-width');")

    def mode_preview_only(self):
        self.btn_preview.setChecked(True); self.btn_contrast.setChecked(False); self.editor.hide()
        self.preview.page().runJavaScript("document.getElementById('content-body').classList.add('full-width');")

    def show_editor_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction("撤销", self.editor.undo); menu.addAction("重做", self.editor.redo); menu.addSeparator()
        menu.addAction("复制", self.editor.copy); menu.addAction("剪切", self.editor.cut); menu.addAction("粘贴", self.editor.paste)
        menu.exec(self.editor.mapToGlobal(pos))

    def show_preview_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction("刷新", self.update_preview_content).exec(self.preview.mapToGlobal(pos))

    def insert_format(self, text):
        cursor = self.editor.textCursor(); cursor.insertText(text); self.editor.setFocus()

    def maybe_save(self):
        if not self.editor.document().isModified():
            return True
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("未保存的更改")
        msg_box.setText("当前文档已被修改，是否要在进行操作前保存？")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        btn_save = msg_box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        btn_discard = msg_box.addButton("不保存", QMessageBox.ButtonRole.DestructiveRole)
        btn_cancel = msg_box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()
        if msg_box.clickedButton() == btn_save:
            return self.save_file()
        elif msg_box.clickedButton() == btn_cancel:
            return False
        return True

    def new_file(self):
        if not self.maybe_save(): return
        self.editor.clear(); self.current_file = None; self.editor.document().setModified(False); self.update_title("未命名 - MD 编辑器")
        if self.file_watcher.files():
            self.file_watcher.removePaths(self.file_watcher.files())

    def open_file(self):
        if not self.maybe_save(): return
        last_dir = self.settings.get_setting('last_open_dir', '')
        if not last_dir or not os.path.isdir(last_dir):
            last_dir = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Desktop')
        path, _ = QFileDialog.getOpenFileName(self, "打开", last_dir, "Markdown (*.md)")
        if path: 
            new_dir = os.path.dirname(path)
            self.settings.set_setting('last_open_dir', new_dir)
            self.open_file_by_path(path)

    def open_file_by_path(self, path):
        if not os.path.exists(path): return
        try:
            with open(path, 'r', encoding='utf-8') as f: content = f.read()
        except UnicodeDecodeError:
            try:
                with open(path, 'r', encoding='gbk') as f: content = f.read()
            except Exception as e:
                self.show_custom_msg("解析失败", f"无法识别此文件的编码格式:\n{str(e)}", QMessageBox.Icon.Critical)
                return
        except Exception as e:
            self.show_custom_msg("打开失败", f"读取文件时发生未知错误:\n{str(e)}", QMessageBox.Icon.Critical)
            return
            
        self.editor.setPlainText(content)
        self.current_file = path
        self.editor.document().setModified(False)
        self.update_title(f"{os.path.basename(path)} - MD 编辑器")
        self.start_watching_file(path)

    def start_watching_file(self, path):
        if self.file_watcher.files():
            self.file_watcher.removePaths(self.file_watcher.files())
        if path and os.path.exists(path):
            self.file_watcher.addPath(path)

    def on_external_file_change(self, path):
        if not self.current_file or path != self.current_file:
            return
        if self.editor.document().isModified():
            return
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f: content = f.read()
        except:
            return
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        selected = cursor.selectedText()
        if selected == content:
            return
        self.pause_ui_updates = 5
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        self.start_watching_file(path)

    def save_file(self):
        if not self.current_file:
            last_dir = self.settings.get_setting('last_open_dir', '')
            if not last_dir or not os.path.isdir(last_dir):
                last_dir = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Desktop')
            path, _ = QFileDialog.getSaveFileName(self, "保存", os.path.join(last_dir, "未命名.md"), "Markdown (*.md)")
            if not path: return False
            self.current_file = path
            new_dir = os.path.dirname(path)
            self.settings.set_setting('last_open_dir', new_dir)
        try:
            with open(self.current_file, 'w', encoding='utf-8') as f: f.write(self.editor.toPlainText())
            self.editor.document().setModified(False)
            self.update_title(f"{os.path.basename(self.current_file)} - MD 编辑器")
            self.save_status_label.setText(" (已保存)")
            self.pause_ui_updates = 2
            self.autosave_countdown = self.autosave_interval_val
            self.start_watching_file(self.current_file)
            return True
        except Exception as e:
            self.show_custom_msg("错误", f"保存失败: {str(e)}", QMessageBox.Icon.Critical)
            return False

    def closeEvent(self, event):
        if self.maybe_save():
            event.ignore()
            self.hide()
        else:
            event.ignore()

if __name__ == '__main__':
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setWindowTitle("程序崩溃日志")
        msg.setText("很抱歉，程序遇到了未处理的错误导致崩溃：\n(请查看下方详细信息)")
        msg.setDetailedText(error_msg)
        msg.exec()
        
    sys.excepthook = handle_exception

    if getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(sys.executable))
    else:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

    os.environ["no_proxy"] = "*"
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-proxy-server"
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    clean_old_updates()
    
    SERVER_NAME = "QsSama_MDEditor_Unique_Server"
    
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    
    if socket.waitForConnected(500):
        if len(sys.argv) > 1:
            file_path = os.path.abspath(sys.argv[1])
            socket.write(file_path.encode('utf-8'))
            socket.waitForBytesWritten(1000)
        sys.exit(0)
        
    server = QLocalServer()
    server.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    
    editor = MarkdownEditor()
    
    def handle_new_connection():
        client = server.nextPendingConnection()
        if client.waitForReadyRead(1000):
            msg = client.readAll().data().decode('utf-8')
            if msg:
                editor.open_file_from_external(msg)
        client.disconnectFromServer()
        
    server.newConnection.connect(handle_new_connection)
    
    if os.path.exists(editor.icon_path):
        app.setWindowIcon(QIcon(editor.icon_path))
    
    if len(sys.argv) > 1:
        file_path = os.path.abspath(sys.argv[1])
        if file_path.lower().endswith('.md'):
            editor.open_file_by_path(file_path)
        
    editor.show()
    sys.exit(app.exec())