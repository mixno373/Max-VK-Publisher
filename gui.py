# gui.py
import asyncio
import threading
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QMessageBox,
    QComboBox, QTextEdit, QFormLayout, QInputDialog, QCheckBox, QScrollArea,
)
from PySide6.QtCore import Qt, Signal

from database import (
    init_db, get_pending_users, approve_user, ignore_user, get_all_users,
    get_setting, set_setting, list_targets, upsert_target, delete_target,
    list_categories, get_category, save_category, delete_category,
)
import max_bot


class MainWindow(QMainWindow):
    bot_status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAX Publisher — один бот, много каналов")
        self.resize(1000, 720)
        init_db()
        self.bot_thread = None
        self.bot_loop = None

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self._build_status_tab()
        self._build_settings_tab()
        self._build_targets_tab()
        self._build_categories_tab()
        self._build_users_tab()

        self.bot_status_changed.connect(self.status_label.setText)

    # ===== Статус =====
    def _build_status_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        self.status_label = QLabel("🔴 Бот остановлен")
        self.status_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        l.addWidget(self.status_label)
        self.max_status = QLabel("📡 MAX: не подключён")
        l.addWidget(self.max_status)

        h = QHBoxLayout()
        b1 = QPushButton("▶ Запустить"); b1.clicked.connect(self.start_bot)
        b2 = QPushButton("⏹ Остановить"); b2.clicked.connect(self.stop_bot)
        h.addWidget(b1); h.addWidget(b2)
        l.addLayout(h); l.addStretch()
        self.tabs.addTab(w, "Статус")

    def start_bot(self):
        token = get_setting("max_token")
        if not token:
            QMessageBox.warning(self, "Ошибка", "Укажите токен MAX во вкладке «Настройки».")
            return
        if self.bot_thread and self.bot_thread.is_alive():
            QMessageBox.information(self, "Бот", "Уже запущен.")
            return

        self.bot_status_changed.emit("🟡 Запуск...")

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.bot_loop = loop
            try:
                loop.run_until_complete(max_bot.start_bot(token))
            except Exception as e:
                self.bot_status_changed.emit(f"🔴 Ошибка: {e}")
            finally:
                loop.close()

        self.bot_thread = threading.Thread(target=runner, daemon=True)
        self.bot_thread.start()
        self.bot_status_changed.emit("🟢 Бот запущен")
        self.max_status.setText("📡 MAX: подключён (polling)")

    def stop_bot(self):
        if self.bot_loop and self.bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(max_bot.stop_bot(), self.bot_loop)
        self.bot_status_changed.emit("🔴 Бот остановлен")
        self.max_status.setText("📡 MAX: не подключён")

    # ===== Настройки (только MAX-токен) =====
    def _build_settings_tab(self):
        w = QWidget(); f = QFormLayout(w)
        self.max_token_edit = QLineEdit(get_setting("max_token"))
        self.ds_key_edit = QLineEdit(get_setting("deepseek_api_key"))
        f.addRow("Токен MAX-бота (глобальный):", self.max_token_edit)
        f.addRow("DeepSeek API-ключ:", self.ds_key_edit)
        b = QPushButton("💾 Сохранить"); b.clicked.connect(self._save_settings)
        f.addRow(b)
        self.tabs.addTab(w, "Настройки")

    def _save_settings(self):
        set_setting("max_token", self.max_token_edit.text().strip())
        set_setting("deepseek_api_key", self.ds_key_edit.text().strip())
        QMessageBox.information(self, "OK", "Настройки сохранены.")

    # ===== Каналы назначения =====
    def _build_targets_tab(self):
        w = QWidget(); l = QVBoxLayout(w)

        self.type_combo = QComboBox(); self.type_combo.addItems(["vk", "max", "rutube"])
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        l.addWidget(QLabel("Тип канала:")); l.addWidget(self.type_combo)

        self.targets_list = QListWidget()
        self.targets_list.currentItemChanged.connect(self._load_target_into_form)
        l.addWidget(self.targets_list)

        # --- Поля под каждый тип ---
        self.t_name = QLineEdit()

        # VK
        self.t_vk_token = QLineEdit()
        self.t_vk_group_id = QLineEdit()

        # MAX
        self.t_max_chat_id = QLineEdit()

        # Rutube
        self.t_rutube_token = QLineEdit()
        self.t_rutube_channel_id = QLineEdit()

        self._form = QFormLayout()
        self._form.addRow("Имя:", self.t_name)
        self._form.addRow("VK token:", self.t_vk_token)
        self._form.addRow("VK group_id:", self.t_vk_group_id)
        self._form.addRow("MAX chat_id:", self.t_max_chat_id)
        self._form.addRow("Rutube token:", self.t_rutube_token)
        self._form.addRow("Rutube channel_id:", self.t_rutube_channel_id)
        l.addLayout(self._form)

        h = QHBoxLayout()
        b_save = QPushButton("➕ Добавить/Сохранить"); b_save.clicked.connect(self._save_target)
        b_del = QPushButton("🗑 Удалить"); b_del.clicked.connect(self._delete_target)
        b_clear = QPushButton("✖ Очистить форму"); b_clear.clicked.connect(self._clear_form)
        h.addWidget(b_save); h.addWidget(b_del); h.addWidget(b_clear)
        l.addLayout(h)

        self._current_target_id = None
        self._on_type_changed(self.type_combo.currentText())
        self._refresh_targets()
        self.tabs.addTab(w, "Каналы")


    def _on_type_changed(self, tp):
        """Показываем только поля, относящиеся к текущему типу канала."""
        show_vk = tp == "vk"
        show_max = tp == "max"
        show_rutube = tp == "rutube"

        for widget, visible in [
            (self.t_vk_token, show_vk),
            (self.t_vk_group_id, show_vk),
            (self.t_max_chat_id, show_max),
            (self.t_rutube_token, show_rutube),
            (self.t_rutube_channel_id, show_rutube),
        ]:
            # Скрываем и подпись, и поле
            for role in (QFormLayout.LabelRole, QFormLayout.FieldRole):
                item = self._form.itemAt(self._form.getWidgetPosition(widget)[0], role) \
                    if hasattr(self._form, "getWidgetPosition") else None
            widget.setVisible(visible)

        self._current_target_id = None
        self._refresh_targets()


    def _refresh_targets(self):
        self.targets_list.clear()
        for t in list_targets(self.type_combo.currentText()):
            # В списке показываем, что настроено
            if t["type"] == "vk":
                tail = f"group_id={t['vk_group_id']}"
            elif t["type"] == "max":
                tail = f"chat_id={t['max_chat_id']}"
            else:
                tail = f"channel_id={t['rutube_channel_id']}"
            it = QListWidgetItem(f"#{t['id']} {t['name']} ({tail})")
            it.setData(Qt.UserRole, t["id"])
            self.targets_list.addItem(it)


    def _load_target_into_form(self, item, _):
        if not item:
            return
        tid = item.data(Qt.UserRole)
        t = get_target(tid)
        if not t:
            return
        self._current_target_id = tid
        self.t_name.setText(t["name"] or "")
        self.t_vk_token.setText(t["vk_token"] or "")
        self.t_vk_group_id.setText(t["vk_group_id"] or "")
        self.t_max_chat_id.setText(t["max_chat_id"] or "")
        self.t_rutube_token.setText(t["rutube_token"] or "")
        self.t_rutube_channel_id.setText(t["rutube_channel_id"] or "")


    def _save_target(self):
        tp = self.type_combo.currentText()
        name = self.t_name.text().strip()
        if not name:
            QMessageBox.warning(self, "!", "Укажите имя канала.")
            return

        kwargs = {}
        if tp == "vk":
            kwargs = {
                "vk_token": self.t_vk_token.text().strip() or None,
                "vk_group_id": self.t_vk_group_id.text().strip() or None,
            }
        elif tp == "max":
            kwargs = {"max_chat_id": self.t_max_chat_id.text().strip() or None}
        elif tp == "rutube":
            kwargs = {
                "rutube_token": self.t_rutube_token.text().strip() or None,
                "rutube_channel_id": self.t_rutube_channel_id.text().strip() or None,
            }

        upsert_target(type_=tp, name=name, target_id=self._current_target_id, **kwargs)
        self._current_target_id = None
        self._refresh_targets()
        QMessageBox.information(self, "OK", "Канал сохранён.")


    def _delete_target(self):
        item = self.targets_list.currentItem()
        if not item:
            return
        tid = item.data(Qt.UserRole)
        if QMessageBox.question(self, "?", f"Удалить канал #{tid}?") == QMessageBox.Yes:
            delete_target(tid)
            self._current_target_id = None
            self._refresh_targets()


    def _clear_form(self):
        for w in (self.t_name, self.t_vk_token, self.t_vk_group_id,
                self.t_max_chat_id, self.t_rutube_token, self.t_rutube_channel_id):
            w.clear()
        self._current_target_id = None
        
    # ===== Категории =====
    def _build_categories_tab(self):
        w = QWidget(); l = QVBoxLayout(w)

        row = QHBoxLayout()
        self.cat_combo = QComboBox()
        self.cat_combo.currentTextChanged.connect(self._load_category)
        row.addWidget(QLabel("Категория:")); row.addWidget(self.cat_combo)
        b_new = QPushButton("➕ Новая")
        b_new.clicked.connect(self._new_category)
        b_del = QPushButton("🗑 Удалить")
        b_del.clicked.connect(self._delete_category)
        row.addWidget(b_new); row.addWidget(b_del)
        l.addLayout(row)

        l.addWidget(QLabel("Ссылки для добавления в конце поста (по одной на строку):"))
        self.links_edit = QTextEdit(); l.addWidget(self.links_edit)

        l.addWidget(QLabel("Каналы назначения (какие использовать в этой категории):"))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); self.tgt_checks_layout = QVBoxLayout(inner)
        scroll.setWidget(inner); l.addWidget(scroll)

        b_save = QPushButton("💾 Сохранить категорию")
        b_save.clicked.connect(self._save_category)
        l.addWidget(b_save)

        self._target_checkboxes = []
        self._refresh_cat_combo()
        self.tabs.addTab(w, "Категории")

    def _refresh_cat_combo(self):
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        for c in list_categories():
            self.cat_combo.addItem(c["name"])
        self.cat_combo.blockSignals(False)
        if self.cat_combo.count():
            self._load_category(self.cat_combo.currentText())

    def _load_category(self, name):
        cat = get_category(name) if name else None
        self.links_edit.setPlainText("\n".join(cat["links"]) if cat else "")

        # Пересоздать чекбоксы
        while self.tgt_checks_layout.count():
            it = self.tgt_checks_layout.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._target_checkboxes = []

        selected_ids = {t["id"] for t in (cat["targets"] if cat else [])}
        for t in list_targets():
            cb = QCheckBox(f"[{t['type'].upper()}] {t['name']}")
            cb.setChecked(t["id"] in selected_ids)
            cb.setProperty("target_id", t["id"])
            self.tgt_checks_layout.addWidget(cb)
            self._target_checkboxes.append(cb)
        self.tgt_checks_layout.addStretch()

    def _new_category(self):
        name, ok = QInputDialog.getText(self, "Новая категория", "Название:")
        if ok and name.strip():
            save_category(name.strip(), [], [])
            self._refresh_cat_combo()

    def _delete_category(self):
        name = self.cat_combo.currentText()
        if not name: return
        if QMessageBox.question(self, "?", f"Удалить категорию «{name}»?") == QMessageBox.Yes:
            delete_category(name)
            self._refresh_cat_combo()

    def _save_category(self):
        name = self.cat_combo.currentText()
        if not name:
            return
        links = [x.strip() for x in self.links_edit.toPlainText().splitlines() if x.strip()]
        target_ids = [cb.property("target_id") for cb in self._target_checkboxes if cb.isChecked()]
        save_category(name, links, target_ids)
        QMessageBox.information(self, "OK", f"Категория «{name}» сохранена.")

    # ===== Пользователи =====
    def _build_users_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("⏳ Ожидают подтверждения:"))
        self.pending_list = QListWidget(); l.addWidget(self.pending_list)
        h = QHBoxLayout()
        b1 = QPushButton("✅ Автор"); b1.clicked.connect(lambda: self._approve("author"))
        b2 = QPushButton("👑 Админ"); b2.clicked.connect(lambda: self._approve("admin"))
        b3 = QPushButton("❌ Игнорировать"); b3.clicked.connect(self._ignore)
        h.addWidget(b1); h.addWidget(b2); h.addWidget(b3)
        l.addLayout(h)

        l.addWidget(QLabel("👥 Все пользователи:"))
        self.all_users_list = QListWidget(); l.addWidget(self.all_users_list)
        self._refresh_users()
        self.tabs.addTab(w, "Пользователи")

    def _refresh_users(self):
        self.pending_list.clear()
        for u in get_pending_users():
            it = QListWidgetItem(f"{u['user_id']} @{u['username']}")
            it.setData(Qt.UserRole, u["user_id"])
            self.pending_list.addItem(it)
        self.all_users_list.clear()
        for u in get_all_users():
            self.all_users_list.addItem(f"{u['user_id']} @{u['username']} — {u['role']}")

    def _approve(self, role):
        it = self.pending_list.currentItem()
        if not it: return
        approve_user(it.data(Qt.UserRole), role)
        self._refresh_users()

    def _ignore(self):
        it = self.pending_list.currentItem()
        if not it: return
        ignore_user(it.data(Qt.UserRole))
        self._refresh_users()


def run_gui():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()