/* Iris — extension preferences (GNOME 48-50, Adw).
 *
 * These settings control the extension's *presentation* only. The security
 * behaviour lives in /etc/iris/config.toml and the PAM stack; nothing here can
 * weaken or strengthen authentication, which is why none of it needs root.
 */

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gio from 'gi://Gio';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class IrisPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: 'Iris',
            icon_name: 'auth-fingerprint-symbolic',
        });
        window.add(page);

        /* ---------------------------------------------------------- display */
        const display = new Adw.PreferencesGroup({
            title: 'Display',
            description: 'Where Iris shows its status.',
        });
        page.add(display);

        const indicator = new Adw.SwitchRow({
            title: 'Top bar indicator',
            subtitle: 'Show daemon health and enrolment status in the panel',
        });
        display.add(indicator);
        settings.bind('show-indicator', indicator, 'active', Gio.SettingsBindFlags.DEFAULT);

        const lockRow = new Adw.SwitchRow({
            title: 'Hint on the lock screen',
            subtitle: 'Explain the pause while your face is being checked',
        });
        display.add(lockRow);
        settings.bind('show-on-lock-screen', lockRow, 'active', Gio.SettingsBindFlags.DEFAULT);

        const loginRow = new Adw.SwitchRow({
            title: 'Hint on the login screen',
            subtitle: 'Requires the extension to be enabled for GDM',
        });
        display.add(loginRow);
        settings.bind('show-on-login-screen', loginRow, 'active', Gio.SettingsBindFlags.DEFAULT);

        /* ---------------------------------------------------------- timing */
        const timing = new Adw.PreferencesGroup({
            title: 'Timing',
            description: 'Match this to auth.timeout in /etc/iris/config.toml so the ' +
                         'animation stops when the real attempt does.',
        });
        page.add(timing);

        const timeout = new Adw.SpinRow({
            title: 'Scanning hint duration',
            subtitle: 'Seconds before the hint settles to “Enter your password”',
            adjustment: new Gtk.Adjustment({
                lower: 3, upper: 60, step_increment: 1, page_increment: 5,
                value: settings.get_int('hint-timeout'),
            }),
        });
        timing.add(timeout);
        settings.bind('hint-timeout', timeout, 'value', Gio.SettingsBindFlags.DEFAULT);

        /* -------------------------------------------------------- security */
        const security = new Adw.PreferencesGroup({
            title: 'About face authentication',
        });
        page.add(security);

        const explain = new Adw.ActionRow({
            title: 'This extension does not authenticate you',
            subtitle: 'It only shows status. The decision to unlock is made by PAM, ' +
                      'using the root-owned Iris daemon. Turning these switches off ' +
                      'hides the interface; it does not disable face unlock.',
        });
        explain.set_subtitle_lines(0);
        security.add(explain);

        const disableRow = new Adw.ActionRow({
            title: 'To actually turn face unlock off',
            subtitle: 'sudo iris config set auth.enabled false',
        });
        disableRow.set_subtitle_lines(0);
        disableRow.add_css_class('property');
        security.add(disableRow);

        const docs = new Adw.ActionRow({
            title: 'Security notes',
            subtitle: '/usr/share/doc/iris/SECURITY.md',
        });
        docs.set_subtitle_lines(0);
        security.add(docs);
    }
}
