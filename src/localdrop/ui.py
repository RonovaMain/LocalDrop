"""Flet UI (dark theme). All blocking work runs in threads; the HTTP server never touches the UI directly."""

from __future__ import annotations

import asyncio
import queue
import time

import flet as ft

from . import APP_NAME, AUTHOR, GITHUB, PACKAGE_ID, __version__
from .archives import PICKER_EXTENSIONS
from .netinfo import local_ips
from .projects import ImportErrorLD, Library, Project
from .qr import make_qr_png
from .server import LogEntry, ServerStartError, SiteServer
from .util import clock, human_duration, human_size, load_settings, save_settings

try:  # optional, desktop only: native drag-and-drop from the OS (needs one `flet build`)
    import flet_dropzone as ftd
except Exception:  # pragma: no cover
    ftd = None

BG, CARD, CARD2 = "#0B0F1A", "#141A2B", "#1B2338"
TEXT, MUTED = "#E6E9F2", "#7D869F"
ACCENT, OK, ERR, WARN = "#7C5CFF", "#34D399", "#F87171", "#FBBF24"
LOG_MAX = 300
TYPE_TITLES = {"html": "HTML", "js": "JS/TS", "css": "CSS", "media": "Медиа", "fonts": "Шрифты",
               "json": "JSON", "wasm": "WASM", "other": "Прочее"}


def status_color(code: int) -> str:
    if code >= 500 or code == 0:
        return ERR
    if code >= 400:
        return WARN
    if code >= 300:
        return "#8AB4FF"
    return OK


class LocalDropApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.lib = Library()
        self.settings = load_settings()
        self.servers: dict[str, SiteServer] = {}
        self.selected: str | None = None
        self.tab = 0
        self.ips: list[str] = ["127.0.0.1"]
        self.log_q: queue.Queue = queue.Queue()
        self.mobile = page.platform.is_mobile()
        self.picker = ft.FilePicker()
        self.wakelock = ft.Wakelock()
        page.services.append(self.picker)
        page.services.append(self.wakelock)
        self.st_texts: dict[str, ft.Text] = {}
        self.busy = ft.ProgressBar(visible=False)
        self.log_view = ft.ListView(expand=True, spacing=2, auto_scroll=True)
        self.body = ft.Container(expand=True)
        self.drop_card: ft.Container | None = None

    # ------------------------------------------------------------ helpers
    def _h(self, fn, *args):
        async def handler(e):
            res = fn(*args)
            if asyncio.iscoroutine(res):
                await res
        return handler

    def toast(self, text: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(text)))

    def dialog(self, title: str, content: ft.Control, actions: list[ft.Control] | None = None) -> None:
        close = ft.TextButton(content="Закрыть", on_click=self._h(self.page.pop_dialog))
        self.page.show_dialog(ft.AlertDialog(title=ft.Text(title), content=content,
                                             actions=(actions or []) + [close]))

    def url_for(self, p: Project, ip: str | None = None) -> str:
        host = ip or (self.ips[0] if p.lan else "127.0.0.1")
        return f"http://{host}:{self.servers[p.id].port if p.id in self.servers else p.port}/"

    def current(self) -> Project | None:
        return self.lib.get(self.selected) if self.selected else None

    # ------------------------------------------------------------ build
    async def build(self) -> None:
        page = self.page
        page.title = APP_NAME
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(color_scheme_seed=ACCENT)
        page.dark_theme = ft.Theme(color_scheme_seed=ACCENT)
        page.bgcolor = BG
        page.padding = 0
        if page.platform.is_desktop():
            page.window.width, page.window.height = 480, 860
            page.window.min_width, page.window.min_height = 380, 560
        page.appbar = ft.AppBar(
            title=ft.Text(APP_NAME, weight=ft.FontWeight.BOLD), bgcolor=BG,
            actions=[ft.IconButton(icon=ft.Icons.INFO_OUTLINE, tooltip="О приложении",
                                   on_click=self._h(self.show_about))])
        page.navigation_bar = ft.NavigationBar(
            selected_index=0, on_change=self.on_nav,
            destinations=[ft.NavigationBarDestination(icon=ft.Icons.FOLDER_OPEN, label="Проекты"),
                          ft.NavigationBarDestination(icon=ft.Icons.DNS, label="Сервер"),
                          ft.NavigationBarDestination(icon=ft.Icons.RECEIPT_LONG, label="Лог")])
        page.add(ft.SafeArea(expand=True, content=ft.Column(expand=True, spacing=0,
                                                             controls=[self.busy, self.body])))
        self.render()
        page.run_task(self.pump)
        page.run_task(self.refresh_ips)

    async def on_nav(self, e) -> None:
        self.tab = e.control.selected_index
        self.render()

    def render(self) -> None:
        views = [self.library_view, self.server_view, self.log_panel]
        self.body.content = views[self.tab]()
        self.page.update()

    # ------------------------------------------------------------ library tab
    def library_view(self) -> ft.Control:
        import_card = ft.Container(
            bgcolor=CARD, border_radius=16, padding=16,
            content=ft.Column(spacing=10, controls=[
                ft.Text("Добавить сайт", size=17, weight=ft.FontWeight.BOLD),
                ft.Text("ZIP, TAR.GZ, сохранённая страница (MHTML, webarchive), HTML-файл или папка. Сайт распакуется и сохранится в приложении.",
                        size=13, color=MUTED),
                ft.Row(wrap=True, spacing=8, controls=[
                    ft.Button(content="Файл сайта", icon=ft.Icons.UPLOAD_FILE, on_click=self._h(self.pick_file)),
                    ft.OutlinedButton(content="Папка", icon=ft.Icons.FOLDER_OPEN,
                                      on_click=self._h(self.pick_folder))]),
                ft.Text("Можно перетащить файлы или папку в это окно." if (ftd and not self.mobile)
                        else ("ZIP надёжнее папки: Android ограничивает доступ к папкам." if self.mobile else ""),
                        size=12, color=MUTED)]))
        self.drop_card = import_card
        head: ft.Control = import_card
        if ftd and not self.mobile:
            head = ftd.Dropzone(content=import_card, on_dropped=self.on_dropped,
                                on_entered=self._h(self.drop_hover, True), on_exited=self._h(self.drop_hover, False))
        projects = self.lib.list()
        cards: list[ft.Control] = [self.project_card(p) for p in projects]
        if not projects:
            cards = [ft.Container(padding=24, content=ft.Text("Пока нет сайтов. Добавьте ZIP, HTML или папку выше.",
                                                               color=MUTED, text_align=ft.TextAlign.CENTER))]
        return ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=12,
                         controls=[ft.Container(padding=12, content=ft.Column(spacing=12, controls=[head] + cards))])

    def project_card(self, p: Project) -> ft.Control:
        running = p.id in self.servers
        s = p.stats or {}
        info = f"{human_size(s.get('total_bytes', 0))} · {s.get('file_count', 0)} файлов · {p.entry or 'без index.html'}"
        port = self.servers[p.id].port if running else p.port
        dot = ft.Icon(ft.Icons.CIRCLE, size=12, color=OK if running else MUTED)
        return ft.Container(
            bgcolor=CARD, border_radius=16, padding=14, on_click=self._h(self.open_project, p.id),
            content=ft.Column(spacing=8, controls=[
                ft.Row(controls=[dot, ft.Text(p.name, size=16, weight=ft.FontWeight.BOLD, expand=True,
                                              max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                 ft.Text(f":{port}", color=ACCENT, weight=ft.FontWeight.BOLD)]),
                ft.Text(info, size=12, color=MUTED, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row(spacing=4, controls=[
                    ft.Button(content="Стоп" if running else "Старт",
                              icon=ft.Icons.STOP if running else ft.Icons.PLAY_ARROW,
                              on_click=self._h(self.toggle, p.id)),
                    ft.IconButton(icon=ft.Icons.QR_CODE_2, tooltip="QR для подключения по Wi-Fi",
                                  on_click=self._h(self.show_qr, p.id)),
                    ft.IconButton(icon=ft.Icons.SETTINGS, tooltip="Настройки",
                                  on_click=self._h(self.show_settings, p.id))])]))

    async def open_project(self, pid: str) -> None:
        self.selected = pid
        self.tab = 1
        self.page.navigation_bar.selected_index = 1
        self.render()

    async def drop_hover(self, on: bool) -> None:
        if self.drop_card is not None:
            self.drop_card.bgcolor = CARD2 if on else CARD
            self.drop_card.update()

    # ------------------------------------------------------------ importing
    async def pick_file(self) -> None:
        try:
            if self.mobile:   # Android hides files with extensions it has no MIME type for
                files = await self.picker.pick_files(allow_multiple=True)
            else:
                files = await self.picker.pick_files(allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
                                                     allowed_extensions=PICKER_EXTENSIONS)
        except Exception as exc:
            return self.toast(f"Не удалось открыть выбор файла: {exc}")
        paths = [f.path for f in (files or []) if getattr(f, "path", None)]
        if files and not paths:
            return self.toast("Система не отдала путь к файлу. Попробуйте выбрать файл из другой папки.")
        if paths:
            await self.do_import(paths)

    async def pick_folder(self) -> None:
        try:
            path = await self.picker.get_directory_path()
        except Exception as exc:
            return self.toast(f"Выбор папки недоступен: {exc}")
        if path:
            await self.do_import([path])

    async def on_dropped(self, e) -> None:
        paths = [getattr(f, "path", f) for f in (getattr(e, "files", None) or [])]
        paths = [str(p) for p in paths if p]
        await self.drop_hover(False)
        if paths:
            await self.do_import(paths)

    async def do_import(self, paths: list[str]) -> None:
        self.busy.visible = True
        self.busy.update()
        try:
            res = await asyncio.to_thread(self.lib.import_paths, paths)
        except ImportErrorLD as exc:
            self.busy.visible = False
            self.busy.update()
            return self.toast(str(exc))
        except Exception as exc:
            self.busy.visible = False
            self.busy.update()
            return self.toast(f"Ошибка импорта: {exc}")
        self.busy.visible = False
        self.selected = res.project.id
        self.render()
        self.toast(f"Добавлен сайт «{res.project.name}»")
        if res.warnings:
            self.dialog("Обратите внимание", ft.Column(tight=True, controls=[ft.Text(w) for w in res.warnings]))

    # ------------------------------------------------------------ server control
    async def refresh_ips(self) -> None:
        self.ips = await asyncio.to_thread(local_ips)

    async def toggle(self, pid: str) -> None:
        if pid in self.servers:
            await self.stop(pid)
        else:
            await self.start(pid)

    async def start(self, pid: str) -> bool:
        p = self.lib.get(pid)
        if not p or pid in self.servers:
            return pid in self.servers
        srv = SiteServer(str(self.lib.serve_root(p)), self.lib.entry_name(p), p.port, name=p.name,
                         host="0.0.0.0" if p.lan else "127.0.0.1", spa=p.spa, live_reload=p.live_reload,
                         cors=p.cors, data_file=str(self.lib.data_file(p)),
                         on_log=lambda e, n=p.name: self.log_q.put((n, e)))
        try:
            srv.start()
        except ServerStartError as exc:
            self.toast(str(exc))
            return False
        self.servers[pid] = srv
        await self.refresh_ips()
        self.log_q.put((p.name, LogEntry(time.time(), "—", f"сервер запущен на порту {srv.port}", 0, 0, 0, "", "")))
        await self.sync_wakelock()
        self.render()
        return True

    async def stop(self, pid: str) -> None:
        srv = self.servers.pop(pid, None)
        if srv:
            await asyncio.to_thread(srv.stop)
        await self.sync_wakelock()
        self.render()

    async def sync_wakelock(self) -> None:
        try:
            if self.servers and self.settings.get("keep_awake", True) and self.mobile:
                await self.wakelock.enable()
            else:
                await self.wakelock.disable()
        except Exception:
            pass

    # ------------------------------------------------------------ server tab
    def server_view(self) -> ft.Control:
        p = self.current()
        if not p:
            return ft.Container(padding=24, content=ft.Text("Выберите проект на вкладке «Проекты».", color=MUTED))
        srv = self.servers.get(p.id)
        running = srv is not None
        self.st_texts = {}
        controls: list[ft.Control] = [
            ft.Row(controls=[ft.Icon(ft.Icons.CIRCLE, size=12, color=OK if running else MUTED),
                             ft.Text(p.name, size=20, weight=ft.FontWeight.BOLD, expand=True),
                             ft.Text("Работает" if running else "Остановлен", color=OK if running else MUTED)]),
            ft.Button(content="Остановить сервер" if running else "Запустить сервер",
                      icon=ft.Icons.STOP if running else ft.Icons.PLAY_ARROW, on_click=self._h(self.toggle, p.id))]
        if running:
            controls.append(self.url_card(p))
            controls.append(self.qr_card(p))
            controls.append(self.stats_card())
        else:
            controls.append(ft.Container(bgcolor=CARD, border_radius=16, padding=16, content=ft.Text(
                f"Порт {p.port}. Запустите сервер, чтобы получить адрес и QR-код.", color=MUTED)))
        controls.append(self.project_stats_card(p))
        if self.mobile:
            controls.append(ft.Switch(label="Не гасить экран, пока сервер работает",
                                      value=self.settings.get("keep_awake", True), on_change=self.on_keep_awake))
        return ft.Column(expand=True, scroll=ft.ScrollMode.AUTO,
                         controls=[ft.Container(padding=12, content=ft.Column(spacing=12, controls=controls))])

    async def on_keep_awake(self, e) -> None:
        self.settings["keep_awake"] = bool(e.control.value)
        save_settings(self.settings)
        await self.sync_wakelock()

    def url_row(self, url: str, label: str, primary: bool = False) -> ft.Control:
        return ft.Row(controls=[
            ft.Column(expand=True, spacing=0, controls=[
                ft.Text(label, size=11, color=MUTED),
                ft.Text(url, selectable=True, size=16 if primary else 13,
                        weight=ft.FontWeight.BOLD if primary else None, color=TEXT if primary else MUTED)]),
            ft.IconButton(icon=ft.Icons.CONTENT_COPY, tooltip="Копировать", on_click=self._h(self.copy, url)),
            ft.IconButton(icon=ft.Icons.OPEN_IN_BROWSER, tooltip="Открыть в браузере",
                          on_click=self._h(self.open_url, url))])

    def url_card(self, p: Project) -> ft.Control:
        rows: list[ft.Control] = []
        if p.lan:
            rows.append(self.url_row(self.url_for(p), "Адрес в Wi-Fi сети", True))
            for ip in self.ips[1:3]:
                rows.append(self.url_row(self.url_for(p, ip), "Другой адрес"))
        else:
            rows.append(ft.Text("Доступ по Wi-Fi выключен в настройках проекта.", color=WARN, size=13))
        rows.append(self.url_row(self.url_for(p, "127.0.0.1"), "На этом устройстве"))
        return ft.Container(bgcolor=CARD, border_radius=16, padding=14, content=ft.Column(spacing=4, controls=rows))

    def qr_card(self, p: Project) -> ft.Control:
        if not p.lan:
            return ft.Container()
        png = make_qr_png(self.url_for(p))
        if not png:
            return ft.Container(bgcolor=CARD, border_radius=16, padding=14,
                                content=ft.Text("QR недоступен: не установлена библиотека segno.", color=WARN))
        return ft.Container(bgcolor=CARD, border_radius=16, padding=14, content=ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8, controls=[
                ft.Container(bgcolor="#FFFFFF", border_radius=12, padding=8,
                             content=ft.Image(src=png, width=220, height=220)),
                ft.Text("Наведите камеру телефона. Устройства должны быть в одной Wi-Fi сети.",
                        size=12, color=MUTED, text_align=ft.TextAlign.CENTER)]))

    def stats_card(self) -> ft.Control:
        keys = [("requests", "Запросы"), ("bytes", "Трафик"), ("clients", "Клиенты"), ("errors", "Ошибки 4xx/5xx"),
                ("uptime", "Работает"), ("live", "Live Reload")]
        rows = []
        for k, title in keys:
            t = ft.Text("—", weight=ft.FontWeight.BOLD)
            self.st_texts[k] = t
            rows.append(ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                               controls=[ft.Text(title, color=MUTED), t]))
        self.update_stats(only_values=True)
        return ft.Container(bgcolor=CARD, border_radius=16, padding=14,
                            content=ft.Column(spacing=6, controls=[ft.Text("Статистика", weight=ft.FontWeight.BOLD)] + rows))

    def update_stats(self, only_values: bool = False) -> None:
        srv = self.servers.get(self.selected or "")
        if not srv or not self.st_texts:
            return
        s = srv.stats.snapshot()
        vals = {"requests": str(s["requests"]), "bytes": human_size(s["bytes"]), "clients": str(s["clients"]),
                "errors": str(s["errors"]), "uptime": human_duration(s["uptime"]),
                "live": f"{srv.sse_clients} подкл." if srv.live_reload else "выкл."}
        for k, v in vals.items():
            if k in self.st_texts:
                self.st_texts[k].value = v
        if not only_values:
            for t in self.st_texts.values():
                t.update()

    def project_stats_card(self, p: Project) -> ft.Control:
        s = p.stats or {}
        bt = s.get("by_type", {})
        chips = [ft.Text(f"{TYPE_TITLES[k]}: {v['count']}", size=13)
                 for k, v in bt.items() if v.get("count")]
        return ft.Container(bgcolor=CARD, border_radius=16, padding=14, content=ft.Column(spacing=8, controls=[
            ft.Text("Анализ проекта", weight=ft.FontWeight.BOLD),
            ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                ft.Text("Вес", color=MUTED), ft.Text(human_size(s.get("total_bytes", 0)))]),
            ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                ft.Text("Файлов", color=MUTED), ft.Text(str(s.get("file_count", 0)))]),
            ft.Row(wrap=True, spacing=12, controls=chips),
            ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                ft.Text('Пустые ссылки href="#"', color=MUTED),
                ft.Text(str(s.get("empty_links", 0)), color=WARN if s.get("empty_links") else TEXT)]),
            ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                ft.Text("Комментарии TODO", color=MUTED),
                ft.Text(str(s.get("todo_count", 0)), color=WARN if s.get("todo_count") else TEXT)]),
            ft.Row(spacing=4, controls=[
                ft.TextButton(content="Подробнее", on_click=self._h(self.show_details, p.id)),
                ft.TextButton(content="Пересканировать", icon=ft.Icons.REFRESH,
                              on_click=self._h(self.rescan, p.id))])]))

    async def rescan(self, pid: str) -> None:
        p = self.lib.get(pid)
        if p:
            self.busy.visible = True
            self.busy.update()
            await asyncio.to_thread(self.lib.rescan, p)
            self.busy.visible = False
            self.render()

    async def show_details(self, pid: str) -> None:
        p = self.lib.get(pid)
        if not p:
            return
        s = p.stats or {}
        lines: list[ft.Control] = []
        if s.get("empty_link_files"):
            lines.append(ft.Text('href="#" по файлам', weight=ft.FontWeight.BOLD))
            lines += [ft.Text(f"{f}: {n}", size=12, selectable=True) for f, n in s["empty_link_files"]]
        if s.get("todo_samples"):
            lines.append(ft.Text("TODO", weight=ft.FontWeight.BOLD))
            lines += [ft.Text(f"{x['file']}:{x['line']} — {x['text']}", size=12, selectable=True)
                      for x in s["todo_samples"]]
        if s.get("biggest"):
            lines.append(ft.Text("Самые большие файлы", weight=ft.FontWeight.BOLD))
            lines += [ft.Text(f"{f} — {human_size(n)}", size=12, selectable=True) for f, n in s["biggest"]]
        if s.get("truncated"):
            lines.append(ft.Text("Анализ остановлен по времени, часть файлов не проверена.", color=WARN, size=12))
        if not lines:
            lines.append(ft.Text("Замечаний нет.", color=MUTED))
        self.dialog("Детали проекта", ft.Container(width=340, height=360, content=ft.Column(
            tight=True, scroll=ft.ScrollMode.AUTO, spacing=4, controls=lines)))

    # ------------------------------------------------------------ actions
    async def copy(self, text: str) -> None:
        await ft.Clipboard().set(text)
        self.toast("Скопировано")

    async def open_url(self, url: str) -> None:
        try:
            await ft.UrlLauncher().launch_url(url)
        except Exception as exc:
            self.toast(f"Не удалось открыть браузер: {exc}")

    async def show_qr(self, pid: str) -> None:
        p = self.lib.get(pid)
        if not p:
            return
        if pid not in self.servers and not await self.start(pid):
            return
        url = self.url_for(p)
        png = make_qr_png(url)
        body: list[ft.Control] = []
        if png and p.lan:
            body.append(ft.Container(bgcolor="#FFFFFF", border_radius=12, padding=8,
                                     content=ft.Image(src=png, width=240, height=240)))
        body.append(ft.Text(url, selectable=True, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER))
        body.append(ft.Text("Телефон и сервер должны быть в одной Wi-Fi сети." if p.lan
                            else "Доступ по Wi-Fi выключен в настройках проекта.", size=12, color=MUTED,
                            text_align=ft.TextAlign.CENTER))
        self.dialog(p.name, ft.Container(width=290, content=ft.Column(
            tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10, controls=body)),
            [ft.TextButton(content="Копировать", on_click=self._h(self.copy, url))])

    async def show_settings(self, pid: str) -> None:
        p = self.lib.get(pid)
        if not p:
            return
        name = ft.TextField(label="Название", value=p.name, dense=True)
        port = ft.TextField(label="Порт (1024–65535)", value=str(p.port), dense=True,
                            keyboard_type=ft.KeyboardType.NUMBER)
        entry = ft.TextField(label="Точка входа", value=p.entry or "", dense=True, hint_text="index.html")
        spa = ft.Switch(label="SPA-роутинг (fallback на index.html)", value=p.spa)
        live = ft.Switch(label="Live Reload", value=p.live_reload)
        cors = ft.Switch(label="CORS-заголовки", value=p.cors)
        lan = ft.Switch(label="Доступ из Wi-Fi сети", value=p.lan)

        async def save(_):
            try:
                pv = int(port.value)
                if not 1024 <= pv <= 65535:
                    raise ValueError
            except ValueError:
                return self.toast("Порт должен быть числом от 1024 до 65535")
            try:
                p.name = (name.value or p.name).strip()[:60] or p.name
                p.port, p.spa, p.live_reload, p.cors, p.lan = pv, spa.value, live.value, cors.value, lan.value
                if (entry.value or "").strip() != (p.entry or ""):
                    await asyncio.to_thread(self.lib.set_entry, p, entry.value)   # validates first
                self.lib.save(p)
            except ImportErrorLD as exc:
                return self.toast(str(exc))          # nothing was stopped or changed
            was_running = pid in self.servers
            if was_running:
                await self.stop(pid)
            self.page.pop_dialog()
            if was_running:
                await self.start(pid)
            self.render()

        async def delete(_):
            await self.stop(pid)
            self.lib.delete(pid)
            if self.selected == pid:
                self.selected = None
            self.page.pop_dialog()
            self.render()
            self.toast("Сайт удалён")

        self.dialog("Настройки сайта", ft.Container(width=320, content=ft.Column(
            tight=True, scroll=ft.ScrollMode.AUTO, spacing=8,
            controls=[name, port, entry, spa, live, cors, lan,
                      ft.Text("Данные /api/data хранятся в файле localdrop-data.json внутри проекта.",
                              size=11, color=MUTED)])),
            [ft.TextButton(content="Удалить", icon=ft.Icons.DELETE_OUTLINE, on_click=delete),
             ft.Button(content="Сохранить", on_click=save)])

    async def show_about(self) -> None:
        self.dialog(APP_NAME, ft.Column(tight=True, spacing=4, controls=[
            ft.Text(f"Версия {__version__}"), ft.Text(PACKAGE_ID, size=12, color=MUTED),
            ft.Text(f"Автор: {AUTHOR}"), ft.Text(f"GitHub: {GITHUB}", selectable=True),
            ft.Text("Мгновенный хостинг веб-проектов в локальной сети.", size=12, color=MUTED)]))

    # ------------------------------------------------------------ log tab
    def log_panel(self) -> ft.Control:
        return ft.Column(expand=True, spacing=0, controls=[
            ft.Container(padding=8, content=ft.Row(controls=[
                ft.Text("HTTP-запросы", size=17, weight=ft.FontWeight.BOLD, expand=True),
                ft.IconButton(icon=ft.Icons.DELETE_SWEEP, tooltip="Очистить", on_click=self._h(self.clear_log))])),
            ft.Container(expand=True, padding=12, content=self.log_view)])

    async def clear_log(self) -> None:
        self.log_view.controls.clear()
        self.log_view.update()

    def log_row(self, name: str, e: LogEntry) -> ft.Control:
        tag = f"[{name}] " if len(self.servers) > 1 else ""
        if e.method == "—":
            return ft.Text(f"{clock(e.ts)}  {tag}{e.path}", size=12, color=ACCENT)
        note = f"  ({e.note})" if e.note else ""
        txt = f"{clock(e.ts)}  {e.status}  {e.method}  {e.path}  · {human_size(e.size)} · {e.ms:.0f} мс · {e.client}{note}"
        return ft.Text(tag + txt, size=12, color=status_color(e.status), selectable=True)

    async def pump(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            new = 0
            while new < 200:
                try:
                    name, entry = self.log_q.get_nowait()
                except queue.Empty:
                    break
                self.log_view.controls.append(self.log_row(name, entry))
                new += 1
            if len(self.log_view.controls) > LOG_MAX:
                del self.log_view.controls[:-LOG_MAX]
            try:
                if self.tab == 2 and new:
                    self.log_view.update()
                elif self.tab == 1:
                    self.update_stats()
            except Exception:
                pass


async def main(page: ft.Page) -> None:
    await LocalDropApp(page).build()
