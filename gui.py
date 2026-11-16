# gui.py
import asyncio
import threading
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QMessageBox,
    QComboBox, QCheckBox, QTextEdit, QGroupBox, QFormLayout, QSpinBox
)
from PySide6.QtCore import Qt, QTimer, Signal
from database import (
    init_db, get_pending_users, approve_user, ignore_user,
    get_setting, set_setting, get_categories, save_category, get_all_users
)
import max_bot

class MainWindow(QMainWindow):
    bot_status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAX Publisher Bot – Панель управления")
        self.resize(900, 700)

        init_db()
        self.bot_thread = None
        self.bot_loop = None

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self._build_status_tab()
        self._build_settings_tab()
        self._build_users_tab()
        self._build_categories_tab()

        self.bot_status_changed.connect(self._update_status_label)

    # ---------- Вкладка «Статус» ----------
    def _build_status_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        self.status_label = QLabel("🔴 Бот остановлен")
        self.status_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.status_label)

        self.max_status_label = QLabel("📡 MAX: не подключён")
        layout.addWidget(self.max_status_label)

        btn_start = QPushButton("▶ Запустить бота")
        btn_start.clicked.connect(self.start_bot)
        btn_stop = QPushButton("⏹ Остановить бота")
        btn_stop.clicked.connect(self.stop_bot)

        h = QHBoxLayout()
        h.addWidget(btn_start)
        h.addWidget(btn_stop)
        layout.addLayout(h)
        layout.addStretch()
        self.tabs.addTab(w, "Статус")

    def _update_status_label(self, text: str):
        self.status_label.setText(text)

    def start_bot(self):
        token = get_setting("max_token")
        if not token:
            QMessageBox.warning(self, "Ошибка", "Сначала укажите токен MAX во вкладке «Настройки».")
            return

        if self.bot_thread and self.bot_thread.is_alive():
            QMessageBox.information(self, "Бот", "Бот уже запущен.")
            return

        self.bot_status_changed.emit("🟡 Бот запускается...")

        def run_bot():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.bot_loop = loop
            try:
                loop.run_until_complete(max_bot.start_bot(token))
            except Exception as e:
                self.bot_status_changed.emit(f"🔴 Ошибка: {e}")
            finally:
                loop.close()

        self.bot_thread = threading.Thread(target=run_bot, daemon=True)
        self.bot_thread.start()
        self.bot_status_changed.emit("🟢 Бот запущен")
        self.max_status_label.setText("📡 MAX: подключён (polling)")

    def stop_bot(self):
        if self.bot_loop and self.bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(max_bot.stop_bot(), self.bot_loop)
        self.bot_status_changed.emit("🔴 Бот остановлен")
        self.max_status_label.setText("📡 MAX: не подключён")

    # ---------- Вкладка «Настройки» ----------
    def _build_settings_tab(self):
        w = QWidget()
        layout = QFormLayout(w)

        self.max_token_edit = QLineEdit(get_setting("max_token"))
        self.vk_token_edit = QLineEdit(get_setting("vk_token"))
        self.vk_group_edit = QLineEdit(get_setting("vk_group_id"))
        self.rutube_token_edit = QLineEdit(get_setting("rutube_token"))
        self.deepseek_key_edit = QLineEdit(get_setting("deepseek_api_key"))

        layout.addRow("Токен MAX:", self.max_token_edit)
        layout.addRow("Токен VK:", self.vk_token_edit)
        layout.addRow("ID группы VK:", self.vk_group_edit)
        layout.addRow("Токен Rutube:", self.rutube_token_edit)
        layout.addRow("API-ключ DeepSeek:", self.deepseek_key_edit)

        btn_save = QPushButton("💾 Сохранить настройки")
        btn_save.clicked.connect(self.save_settings)
        layout.addRow(btn_save)

        self.tabs.addTab(w, "Настройки")

    def save_settings(self):
        set_setting("max_token", self.max_token_edit.text().strip())
        set_setting("vk_token", self.vk_token_edit.text().strip())
        set_setting("vk_group_id", self.vk_group_edit.text().strip())
        set_setting("rutube_token", self.rutube_token_edit.text().strip())
        set_setting("deepseek_api_key", self.deepseek_key_edit.text().strip())
        QMessageBox.information(self, "Настройки", "Сохранено.")

    # ---------- Вкладка «Пользователи» ----------
    def _build_users_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        self.pending_list = QListWidget()
        self._refresh_pending()
        layout.addWidget(QLabel("⏳ Ожидают подтверждения:"))
        layout.addWidget(self.pending_list)

        h = QHBoxLayout()
        btn_approve = QPushButton("✅ Одобрить (автор)")
        btn_approve.clicked.connect(self.approve_selected)
        btn_ignore = QPushButton("❌ Игнорировать")
        btn_ignore.clicked.connect(self.ignore_selected)
        h.addWidget(btn_approve)
        h.addWidget(btn_ignore)
        layout.addLayout(h)

        layout.addWidget(QLabel("👥 Все пользователи:"))
        self.all_users_list = QListWidget()
        self._refresh_all_users()
        layout.addWidget(self.all_users_list)

        self.tabs.addTab(w, "Пользователи")

    def _refresh_pending(self):
        self.pending_list.clear()
        for u in get_pending_users():
            item = QListWidgetItem(f"{u['user_id']} – @{u['username']}")
            item.setData(Qt.UserRole, u["user_id"])
            self.pending_list.addItem(item)

    def _refresh_all_users(self):
        self.all_users_list.clear()
        for u in get_all_users():
            self.all_users_list.addItem(f"{u['user_id']} – @{u['username']} – {u['role']}")

    def approve_selected(self):
        item = self.pending_list.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        approve_user(uid, "author")
        self._refresh_pending()
        self._refresh_all_users()

    def ignore_selected(self):
        item = self.pending_list.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        ignore_user(uid)
        self._refresh_pending()
        self._refresh_all_users()

    # ---------- Вкладка «Категории» ----------
    def _build_categories_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        self.cat_combo = QComboBox()
        self.cat_combo.addItems(get_categories().keys())
        self.cat_combo.currentTextChanged.connect(self.load_category)
        layout.addWidget(QLabel("Категория:"))
        layout.addWidget(self.cat_combo)

        self.chk_vk = QCheckBox("VK")
        self.chk_max = QCheckBox("MAX")
        self.chk_rutube = QCheckBox("Rutube")
        h = QHBoxLayout()
        h.addWidget(self.chk_vk)
        h.addWidget(self.chk_max)
        h.addWidget(self.chk_rutube)
        layout.addLayout(h)

        self.links_edit = QTextEdit()
        self.links_edit.setPlaceholderText("Ссылки для добавления в конце поста (по одной на строку)")
        layout.addWidget(QLabel("Ссылки:"))
        layout.addWidget(self.links_edit)

        btn_save = QPushButton("💾 Сохранить категорию")
        btn_save.clicked.connect(self.save_category_ui)
        layout.addWidget(btn_save)

        self.load_category(self.cat_combo.currentText())
        self.tabs.addTab(w, "Категории")

    def load_category(self, name: str):
        cats = get_categories()
        data = cats.get(name, {"channels": [], "links": []})
        self.chk_vk.setChecked("vk" in data["channels"])
        self.chk_max.setChecked("max" in data["channels"])
        self.chk_rutube.setChecked("rutube" in data["channels"])
        self.links_edit.setPlainText("\n".join(data["links"]))

    def save_category_ui(self):
        name = self.cat_combo.currentText()
        channels = []
        if self.chk_vk.isChecked():
            channels.append("vk")
        if self.chk_max.isChecked():
            channels.append("max")
        if self.chk_rutube.isChecked():
            channels.append("rutube")
        links = [l.strip() for l in self.links_edit.toPlainText().splitlines() if l.strip()]
        save_category(name, channels, links)
        QMessageBox.information(self, "Категории", f"Категория «{name}» сохранена.")

def run_gui():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()