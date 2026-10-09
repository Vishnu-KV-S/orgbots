import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../session.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// Signing in, for a server with members. Rather than re-implement sign-in, the app
/// opens the server's own `/signin` page — invitation links and single sign-on both
/// work there already — in a web view. When that page sends the browser on into the
/// web app, sign-in is done: the app takes the session cookie it set and carries on.
///
/// [link] (from an `orgbots://signin?link=…` deep link, or a pasted sign-in link)
/// opens that link directly.
class SignInScreen extends StatefulWidget {
  const SignInScreen({super.key, this.link});
  final String? link;

  @override
  State<SignInScreen> createState() => _SignInScreenState();
}

class _SignInScreenState extends State<SignInScreen> {
  WebViewController? _web;
  bool _loading = true;
  bool _done = false;
  late String? _link = widget.link;

  Session get _session => SessionScope.read(context);

  Uri get _start {
    final base = Uri.parse('${_session.server}/signin');
    return base.replace(queryParameters: {'return_to': '/', 'link': ?_link});
  }

  @override
  void initState() {
    super.initState();
    if (kIsWeb) return;
    _web = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onNavigationRequest: (request) =>
              _isSignedIn(request.url) ? NavigationDecision.prevent : NavigationDecision.navigate,
          onUrlChange: (change) => _isSignedIn(change.url ?? ''),
          onPageFinished: (_) => mounted ? setState(() => _loading = false) : null,
        ),
      )
      ..loadRequest(_start);
  }

  /// Navigation inside `/signin` and the API's SSO callback is sign-in; identity
  /// providers are other hosts and load normally. Anything else on the server means
  /// the page has let us in.
  bool _isSignedIn(String url) {
    final uri = Uri.tryParse(url);
    final server = Uri.parse(_session.server);
    if (uri == null || uri.host != server.host || uri.port != server.port) {
      return false;
    }
    if (uri.path.startsWith('/signin') || uri.path.startsWith('/rt/')) {
      return false;
    }
    if (!_done) {
      _done = true;
      _finish();
    }
    return true;
  }

  Future<void> _finish() async {
    final ok = await _session.adoptWebSession();
    if (ok || !mounted) return;
    _done = false;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('The server didn’t recognise this phone’s session. Try again.')),
    );
  }

  Future<void> _paste() async {
    final text = (await Clipboard.getData('text/plain'))?.text?.trim() ?? '';
    final token =
        Uri.tryParse(text)?.queryParameters['link'] ??
        (RegExp(r'^[A-Za-z0-9_\-.]{16,}$').hasMatch(text) ? text : null);
    if (!mounted) return;
    if (token == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Copy the sign-in link an admin sent you, then tap paste again.'),
        ),
      );
      return;
    }
    setState(() {
      _link = token;
      _loading = true;
    });
    await _web?.loadRequest(_start);
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final host = Uri.parse(_session.server).host;
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Change server',
          icon: const Icon(Icons.arrow_back),
          onPressed: _session.disconnect,
        ),
        title: Column(
          children: [
            const Text('Sign in', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
            Text(host, style: TextStyle(fontSize: 11.5, color: p.textFaint)),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Paste a sign-in link',
            icon: const Icon(Icons.content_paste),
            onPressed: _paste,
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1),
          child: Divider(height: 1, color: p.line),
        ),
      ),
      body: kIsWeb
          ? Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  spacing: 12,
                  children: [
                    Text(
                      'Sign in on the web app, then come back.',
                      style: TextStyle(color: p.textDim),
                    ),
                    PillButton(
                      label: 'Open sign-in',
                      expand: false,
                      onPressed: () => launchUrl(_start),
                    ),
                    PillButton(
                      label: 'I’ve signed in',
                      kind: ButtonKind.ghost,
                      expand: false,
                      onPressed: () => _session.refresh(),
                    ),
                  ],
                ),
              ),
            )
          : Stack(
              children: [
                WebViewWidget(controller: _web!),
                if (_loading)
                  ColoredBox(
                    color: p.bg,
                    child: const Center(child: CircularProgressIndicator()),
                  ),
              ],
            ),
    );
  }
}
