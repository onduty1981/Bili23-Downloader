from PySide6.QtCore import QStandardPaths

from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import logging
import time
import sys
import os

# --------- Logging Configuration ---------

appdata_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)

log_path = Path(appdata_path) / "Bili23 Downloader" / "logs" / "app.log"
log_path.parent.mkdir(parents = True, exist_ok = True)

logging.basicConfig(
    level = logging.INFO,
    format = "[%(asctime)s] - %(name)s - %(levelname)s: %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.StreamHandler(sys.stdout),
        TimedRotatingFileHandler(log_path, when = "midnight", interval = 1, backupCount = 15, encoding = "utf-8")
    ]
)

# --------- Disable PySide6 Warnings ---------
from PySide6.QtCore import QtMsgType, qInstallMessageHandler

def qt_message_handler(mode, context, message):
    # 忽略特定的 Qt 警告
    if "QFont::setPointSize" in message or "OpenType support missing" in message or "CreateFontFaceFromHDC" in message:
        return
    
    # 其他 Qt 日志转发到 Python logging
    logger = logging.getLogger("Qt")

    if mode == QtMsgType.QtWarningMsg:
        logger.warning(message)

    elif mode == QtMsgType.QtCriticalMsg:
        logger.error(message)

    elif mode == QtMsgType.QtFatalMsg:
        logger.critical(message)

    elif mode == QtMsgType.QtInfoMsg:
        logger.info(message)

    else:
        logger.debug(message)

qInstallMessageHandler(qt_message_handler)

# --------- Imports ---------

from PySide6.QtCore import Qt, QLocale, QTranslator, QByteArray, QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from qfluentwidgets import FluentTranslator

from util.auth import user_manager, cookie_manager
from util.common import config
import res.resources_rc

from gui.interface import MainWindow

SERVER_NAME = "Bili23DownloaderInstance"
INSTANCE_LOCK_NAME = "instance.lock"
INSTANCE_STATE_NAME = "instance.state"
INSTANCE_LOCK_TIMEOUT_MS = 10_000
ACTIVATION_TIMEOUT_MS = 500
HANDSHAKE_TIMEOUT_MS = 5000
INSTANCE_STARTUP_GRACE_MS = 8000
INSTANCE_STATE_STALE_MS = 5_000
INSTANCE_STATE_POLL_INTERVAL_MS = 200
LOCAL_SERVER_RETRY_COUNT = 2

class Application(QApplication):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.window: MainWindow = None
        self.instance_state_timer = QTimer(self)
        self.instance_state_timer.timeout.connect(self.refresh_instance_state)
        self.aboutToQuit.connect(self.cleanup_instance_state)

        self.init_single_instance()

    def init_single_instance(self):
        logger = logging.getLogger(__name__)
        self.lock_path = Path(appdata_path) / "Bili23 Downloader" / "locks" / INSTANCE_LOCK_NAME
        self.instance_state_path = self.lock_path.with_name(INSTANCE_STATE_NAME)

        self.lock_path.parent.mkdir(parents = True, exist_ok = True)

        self.instance_lock = QLockFile(str(self.lock_path))
        self.instance_lock.setStaleLockTime(INSTANCE_LOCK_TIMEOUT_MS)
        self.server = QLocalServer()

        # 优先抢占实例锁，确保同一时间只有一个主进程。
        if self.instance_lock.tryLock(0):
            self.write_instance_state("starting")

            if not self.start_local_server(logger):
                self.cleanup_instance_state()
                self.instance_lock.unlock()
                sys.exit(1)

            self.server.newConnection.connect(self.on_new_connection)
            return

        # 锁被占用时，尝试唤醒已有实例。
        if self.activate_existing_instance(logger):
            sys.exit(0)

        # 如果连不上现有实例，通常意味着锁文件已过期但仍残留，清理后再尝试抢锁。
        self.instance_lock.removeStaleLockFile()

        if not self.instance_lock.tryLock(0):
            logger.error("无法获取实例锁，也无法确认可用的现有实例")
            sys.exit(1)

        self.write_instance_state("starting")

        if not self.start_local_server(logger):
            self.cleanup_instance_state()
            self.instance_lock.unlock()
            sys.exit(1)

        self.server.newConnection.connect(self.on_new_connection)

    def write_instance_state(self, state: str):
        self.instance_state_path.write_text(state, encoding = "utf-8")

    def refresh_instance_state(self):
        if getattr(self, "instance_state_path", None) is None:
            return

        try:
            self.write_instance_state("ready")
        except OSError:
            pass

    def mark_instance_ready(self):
        self.refresh_instance_state()
        self.instance_state_timer.start(1000)

    def cleanup_instance_state(self):
        self.instance_state_timer.stop()

        if hasattr(self, "instance_lock"):
            self.instance_lock.unlock()

        if getattr(self, "instance_state_path", None) is None:
            return

        try:
            self.instance_state_path.unlink()
        except OSError:
            pass

    def is_instance_state_ready(self):
        try:
            if self.instance_state_path.read_text(encoding = "utf-8").strip() != "ready":
                return False

            state_age_ms = (time.time() - self.instance_state_path.stat().st_mtime) * 1000
            return state_age_ms <= INSTANCE_STATE_STALE_MS

        except OSError:
            return False

    def wait_for_existing_instance_ready(self):
        deadline = time.monotonic() + (INSTANCE_STARTUP_GRACE_MS / 1000)

        while time.monotonic() < deadline:
            if self.is_instance_state_ready():
                return True

            time.sleep(INSTANCE_STATE_POLL_INTERVAL_MS / 1000)

        return False

    def start_local_server(self, logger):
        for attempt in range(LOCAL_SERVER_RETRY_COUNT):
            if self.server.listen(SERVER_NAME):
                return True

            if attempt == 0:
                # 监听名残留时，清理后再重试一次。
                QLocalServer.removeServer(SERVER_NAME)

        logger.error("无法启动本地服务器: %s", self.server.errorString())
        return False

    def activate_existing_instance(self, logger):
        self.socket = QLocalSocket()
        self.socket.connectToServer(SERVER_NAME)

        if not self.socket.waitForConnected(ACTIVATION_TIMEOUT_MS):
            return False

        # 已有实例存在，先做一次握手，避免把卡死/未启动完成的实例误判成可用实例
        self.socket.write(QByteArray(b"activate"))
        self.socket.flush()

        if self.socket.waitForBytesWritten(ACTIVATION_TIMEOUT_MS) and self.socket.waitForReadyRead(HANDSHAKE_TIMEOUT_MS):
            response = self.socket.readAll().data()

            if response in (b"ok", b"pong", b"activate"):
                if self.wait_for_existing_instance_ready():
                    logger.warning("另一个实例已在运行，已退出当前实例")
                    return True

                logger.warning("已有实例响应但仍停留在启动状态，继续启动当前实例")
                self.socket.disconnectFromServer()
                return False

            else:
                logger.warning("已有实例返回了未知响应，继续启动当前实例")
                self.socket.disconnectFromServer()
                return False

        logger.warning("已有实例未能及时响应，继续启动当前实例")
        self.socket.disconnectFromServer()
        return False

    def on_new_connection(self):
        socket = self.server.nextPendingConnection()

        if not socket:
            return

        if socket.waitForReadyRead(500):
            data = socket.readAll().data()

            if data == b"activate":
                socket.write(QByteArray(b"ok"))
                socket.flush()
                socket.waitForBytesWritten(500)

                # 激活已有窗口
                if self.window:
                    self.window._activate_window()

            elif data == b"ping":
                socket.write(QByteArray(b"pong"))
                socket.flush()
                socket.waitForBytesWritten(500)

        socket.disconnectFromServer()
        socket.deleteLater()

    def setup_app(self):
        self.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
        
        # 设置默认字体
        self.default_font = self.font()
        self.default_font.setPointSize(10)
        self.default_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)

        self.setFont(self.default_font)

        # 加载翻译文件
        locale: QLocale = config.get(config.language).value

        self.fluent_translator = FluentTranslator(locale)
        self.bili23_translator = QTranslator()
        self.bili23_translator.load(locale, "bili23", ".", ":/bili23/i18n")

        self.installTranslator(self.fluent_translator)
        self.installTranslator(self.bili23_translator)

def _main():
    scaling_value = config.get(config.display_scaling).value

    if scaling_value != "Auto":
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = scaling_value

    app = Application(sys.argv)
    app.setup_app()
    
    # 初始化登录状态等用户信息
    cookie_manager.init_cookie_info()
    user_manager.init_user_info()

    app.window = MainWindow()
    QTimer.singleShot(0, app.mark_instance_ready)

    app.exec()

if __name__ == "__main__":
    _main()
