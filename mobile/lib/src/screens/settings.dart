import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../session.dart';
import '../theme.dart';
import '../widgets/common.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final session = SessionScope.of(context);
    final me = session.me;

    Future<void> leave(Future<void> Function() action) async {
      Navigator.of(context).popUntil((r) => r.isFirst);
      await action();
    }

    Future<void> changeServer() async {
      final sure = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Change server?'),
          content: const Text('You’ll be signed out of this one.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text('Change server', style: TextStyle(color: p.danger)),
            ),
          ],
        ),
      );
      if (sure == true) await leave(session.disconnect);
    }

    return Scaffold(
      appBar: AppBar(
        centerTitle: false,
        automaticallyImplyLeading: false,
        title: Text(
          'Settings',
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.w800, color: p.text),
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: RoundIcon(
              icon: Icons.close,
              tooltip: 'Close',
              filled: true,
              onPressed: () => Navigator.pop(context),
            ),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          if (me != null)
            _Group(
              children: [
                ListTile(
                  contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                  leading: CircleAvatar(
                    radius: 24,
                    backgroundColor: p.raised,
                    child: Text(
                      me.initial,
                      style: TextStyle(color: p.text, fontSize: 20, fontWeight: FontWeight.w700),
                    ),
                  ),
                  title: Text(
                    me.name.isEmpty ? me.email : me.name,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  subtitle: Text('${me.email} · ${me.role}', style: TextStyle(color: p.textDim)),
                ),
              ],
            ),
          const SectionLabel('Server'),
          _Group(
            children: [
              _Row(
                icon: Icons.dns_outlined,
                label: session.server.replaceFirst(RegExp('^https?://'), ''),
                detail: session.mode == 'members' ? 'Team sign-in' : 'Single user',
              ),
              _Row(
                icon: Icons.open_in_new,
                label: 'Open the web app',
                detail: 'Briefs, files, skills, routines and apps',
                onTap: () =>
                    launchUrl(Uri.parse(session.server), mode: LaunchMode.externalApplication),
              ),
              _Row(icon: Icons.swap_horiz, label: 'Change server', onTap: changeServer),
              if (session.mode == 'members')
                _Row(
                  icon: Icons.logout,
                  label: 'Sign out',
                  danger: true,
                  onTap: () => leave(session.signOut),
                ),
            ],
          ),
          const SizedBox(height: 28),
          Text(
            'Orgbots · open source, MIT',
            textAlign: TextAlign.center,
            style: TextStyle(color: p.textFaint, fontSize: 13),
          ),
        ],
      ),
    );
  }
}

class _Group extends StatelessWidget {
  const _Group({required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      decoration: BoxDecoration(
        color: p.surface,
        border: Border.all(color: p.line),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(children: children),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({
    required this.icon,
    required this.label,
    this.detail,
    this.onTap,
    this.danger = false,
  });
  final IconData icon;
  final String label;
  final String? detail;
  final VoidCallback? onTap;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final color = danger ? p.danger : p.text;
    return ListTile(
      onTap: onTap,
      leading: Icon(icon, color: color),
      title: Text(
        label,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(color: color),
      ),
      subtitle: detail == null
          ? null
          : Text(detail!, style: TextStyle(color: p.textFaint, fontSize: 13)),
      trailing: onTap != null && !danger ? Icon(Icons.chevron_right, color: p.textFaint) : null,
    );
  }
}
