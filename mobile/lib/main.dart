import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'src/bot3d/snapshots.dart';
import 'src/screens/connect.dart';
import 'src/screens/home.dart';
import 'src/screens/signin.dart';
import 'src/session.dart';
import 'src/theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  final session = Session()..load();
  runApp(OrgbotsApp(session: session));
}

class OrgbotsApp extends StatelessWidget {
  const OrgbotsApp({super.key, required this.session});
  final Session session;

  @override
  Widget build(BuildContext context) {
    return SessionScope(
      session: session,
      child: MaterialApp(
        title: 'Orgbots',
        debugShowCheckedModeBanner: false,
        theme: themeFor(Palette.light),
        darkTheme: themeFor(Palette.dark),
        themeMode: ThemeMode.system,
        // The renderer for list avatars sits behind every screen: it has to be laid out
        // to draw, but the screens' opaque backgrounds cover it.
        builder: (context, child) {
          // Status and navigation bar icons that read on the theme's background, for the
          // screens without an app bar (an app bar sets its own).
          final dark = Theme.of(context).brightness == Brightness.dark;
          final bg = Palette.of(context).bg;
          return AnnotatedRegion<SystemUiOverlayStyle>(
            value: (dark ? SystemUiOverlayStyle.light : SystemUiOverlayStyle.dark).copyWith(
              statusBarColor: Colors.transparent,
              systemNavigationBarColor: bg,
            ),
            child: Stack(children: [const SnapshotRenderer(), ?child]),
          );
        },
        home: const _Gate(),
      ),
    );
  }
}

/// Sends the person to whichever first step they are missing: a server, then a
/// session. Screens only mount once the saved server is known, since they fetch as
/// they mount. When the session ends (signed out, or expired mid-conversation), any
/// screen still open over the bots list closes, so sign-in is what the person sees.
class _Gate extends StatefulWidget {
  const _Gate();

  @override
  State<_Gate> createState() => _GateState();
}

class _GateState extends State<_Gate> {
  Status? _shown;

  @override
  Widget build(BuildContext context) {
    final session = SessionScope.of(context);
    final status = session.status == Status.offline ? Status.ready : session.status;
    if (_shown == Status.ready && status != Status.ready) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) Navigator.of(context).popUntil((route) => route.isFirst);
      });
    }
    _shown = status;
    final Widget screen = switch (status) {
      Status.loading => const Scaffold(body: Center(child: CircularProgressIndicator())),
      Status.noServer => const ConnectScreen(),
      Status.signedOut => SignInScreen(link: Uri.base.queryParameters['link']),
      Status.ready || Status.offline => const HomeScreen(),
    };
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 250),
      child: KeyedSubtree(key: ValueKey(status), child: screen),
    );
  }
}
