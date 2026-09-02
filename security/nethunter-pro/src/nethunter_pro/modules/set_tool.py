"""Social-Engineer Toolkit template builder (SETFragment).

Faithful to the Android screen: it builds a SET email phishing template
(subject + HTML body) from a base template and your fields, writes it into
SET's templates folder, and launches setoolkit in a terminal for the
interactive part.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..executor import Result, run_async
from ..module import NHModule, register
from ..widgets import ToolRunner

# The base templates SET ships with (as in the Android app).
TEMPLATES = ["Messenger", "Facebook", "Twitter", "Gmail", "Custom"]
SET_TEMPLATE_DIR = "/root/.set/src/templates"


@register
class SocialEngineering(NHModule):
    title = "SET"
    icon = "system-users-symbolic"
    description = "SET email phishing template builder"
    required_tools = ["setoolkit"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        tmpl = Adw.PreferencesGroup(
            title="Phishing email template",
            description="Build a SET template, then launch SET to send it.",
        )
        self.template = Adw.ComboRow(
            title="Base template", model=Gtk.StringList.new(TEMPLATES))
        tmpl.add(self.template)
        self.name = Adw.EntryRow(title="From name")
        tmpl.add(self.name)
        self.subject = Adw.EntryRow(title="Subject")
        tmpl.add(self.subject)
        self.link = Adw.EntryRow(title="Phishing link")
        tmpl.add(self.link)
        self.pic = Adw.EntryRow(title="Account picture URL (optional)")
        tmpl.add(self.pic)
        box.append(tmpl)

        body_group = Adw.PreferencesGroup(title="HTML body")
        box.append(body_group)
        self.body = Gtk.TextView(monospace=True, top_margin=6, bottom_margin=6,
                                 left_margin=6, right_margin=6,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.body.get_buffer().set_text(
            "<html><body>\n"
            "<p>Hello,</p>\n"
            "<p>Please <a href=\"{{LINK}}\">verify your account</a>.</p>\n"
            "</body></html>\n"
        )
        sc = Gtk.ScrolledWindow(child=self.body, min_content_height=140)
        sc.add_css_class("card")
        box.append(sc)

        actions = Adw.PreferencesGroup()
        save = Adw.ActionRow(title="Save template", subtitle=f"into {SET_TEMPLATE_DIR}")
        sb = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        sb.connect("clicked", self._save)
        save.add_suffix(sb)
        actions.add(save)
        launch = Adw.ActionRow(title="Launch SET", subtitle="Opens in the system terminal")
        lb = Gtk.Button(label="Launch", valign=Gtk.Align.CENTER)
        lb.add_css_class("suggested-action")
        lb.connect("clicked", lambda _b: self.runner.run_in_terminal("setoolkit", root=True))
        launch.add_suffix(lb)
        actions.add(launch)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _save(self, _b: Gtk.Button) -> None:
        name = TEMPLATES[self.template.get_selected()]
        subject = self.subject.get_text().strip() or "Notification"
        link = self.link.get_text().strip()
        buf = self.body.get_buffer()
        html = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        html = html.replace("{{LINK}}", link)

        # SET template format: SUBJECT="..."\nHTML="...\nEND"
        content = f'SUBJECT="{subject}"\nHTML="\n{html}\nEND"\n'
        import base64
        b64 = base64.b64encode(content.encode()).decode()
        path = f"{SET_TEMPLATE_DIR}/{name}.template"
        cmd = f"sh -c 'mkdir -p {SET_TEMPLATE_DIR} && echo {b64} | base64 -d > {path}'"

        def done(result: Result) -> None:
            if result.ok:
                self.runner.output.append(f"[saved template to {path}]\n\n")
            else:
                self.runner.output.append(result.stderr + "\n\n")

        self.runner.output.append(f"$ save {name}.template\n")
        run_async(cmd, done, root=True, timeout=15)
