import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../session.dart';
import '../text.dart';
import '../theme.dart';
import 'common.dart';

/// A picture of the bot's screen, kept by the run at a moment that mattered. Masked by
/// the server like everything a bot sees. Tap for full screen.
class ScreenshotThumb extends StatelessWidget {
  const ScreenshotThumb({super.key, required this.botId, required this.screenshotId});
  final String botId;
  final String screenshotId;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final api = SessionScope.of(context).api;
    final image = Image.network(
      api.screenshotUrl(botId, screenshotId).toString(),
      headers: api.authHeaders,
      fit: BoxFit.cover,
      errorBuilder: (_, _, _) => Center(
        child: Text(
          'Screenshot no longer kept',
          style: TextStyle(color: p.textFaint, fontSize: 13),
        ),
      ),
    );
    return Semantics(
      button: true,
      label: 'Screenshot of the bot’s screen. Open full size',
      child: GestureDetector(
        onTap: () => Navigator.of(context).push(
          PageRouteBuilder<void>(
            opaque: false,
            barrierColor: Colors.black,
            pageBuilder: (context, _, _) => Stack(
              children: [
                GestureDetector(
                  onTap: () => Navigator.of(context).pop(),
                  child: InteractiveViewer(
                    maxScale: 5,
                    child: Center(
                      child: Image.network(
                        api.screenshotUrl(botId, screenshotId).toString(),
                        headers: api.authHeaders,
                      ),
                    ),
                  ),
                ),
                SafeArea(
                  child: Align(
                    alignment: Alignment.topRight,
                    child: Padding(
                      padding: const EdgeInsets.all(8),
                      child: IconButton(
                        tooltip: 'Close',
                        color: Colors.white,
                        style: IconButton.styleFrom(backgroundColor: Colors.white12),
                        icon: const Icon(Icons.close),
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
        child: Container(
          margin: const EdgeInsets.only(top: 8),
          decoration: BoxDecoration(
            color: p.raised,
            border: Border.all(color: p.line),
            borderRadius: BorderRadius.circular(Radii.md),
          ),
          clipBehavior: Clip.antiAlias,
          child: AspectRatio(aspectRatio: viewportWidth / viewportHeight, child: image),
        ),
      ),
    );
  }
}

class _Card extends StatelessWidget {
  const _Card({required this.live, required this.children});
  final bool live;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return AnimatedOpacity(
      opacity: live ? 1 : 0.6,
      duration: const Duration(milliseconds: 200),
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: p.surface,
          border: Border.all(color: live ? p.accent : p.line),
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          spacing: 10,
          children: children,
        ),
      ),
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(color: p.accent, borderRadius: BorderRadius.circular(Radii.pill)),
        child: Text(
          text,
          style: TextStyle(color: p.onAccent, fontSize: 11.5, fontWeight: FontWeight.w700),
        ),
      ),
    );
  }
}

/// The bot wants to do something consequential and is waiting. The card shows the
/// operation and its real inputs, built by the runtime from the action itself, and the
/// bot's reasoning underneath as what it is: the bot's account of why.
class ApprovalCard extends StatefulWidget {
  const ApprovalCard({
    super.key,
    required this.botId,
    required this.message,
    required this.live,
    required this.onDecided,
  });
  final String botId;
  final BotMessage message;
  final bool live;
  final VoidCallback onDecided;

  @override
  State<ApprovalCard> createState() => _ApprovalCardState();
}

class _ApprovalCardState extends State<ApprovalCard> {
  String? _busy;
  String? _error;

  Future<void> _answer(String decision) async {
    setState(() {
      _busy = decision;
      _error = null;
    });
    try {
      await SessionScope.read(context).api.decide(widget.botId, widget.message.pendingId, decision);
      await (decision == 'deny' ? HapticFeedback.heavyImpact() : HapticFeedback.mediumImpact());
      widget.onDecided();
    } on ApiError catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final m = widget.message;
    final a = m.action;
    String? text(String k) => a[k] is String && (a[k] as String).isNotEmpty ? a[k] as String : null;
    final rows = <(String, String?)>[
      ('URL', text('url')),
      ('Element', text('element_label')),
      ('Text', a['text'] == null ? null : (a['secret'] == true ? '••••••' : text('text'))),
      ('On page', text('page_url')),
    ].where((r) => r.$2 != null);

    return _Card(
      live: widget.live,
      children: [
        if (widget.live)
          const _Badge('Needs approval')
        else
          Text(
            'DECIDED',
            style: TextStyle(
              color: p.textFaint,
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.8,
            ),
          ),
        Text(
          describeAction(a),
          style: TextStyle(color: p.text, fontSize: 16, fontWeight: FontWeight.w600, height: 1.35),
        ),
        for (final (k, v) in rows)
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(
                width: 66,
                child: Text(k, style: TextStyle(color: p.textFaint, fontSize: 13.5)),
              ),
              Expanded(
                child: Text(
                  v!,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: p.textDim, fontSize: 13.5),
                ),
              ),
            ],
          ),
        if (m.content.isNotEmpty) ...[
          Divider(height: 1, color: p.line),
          Text(m.content, style: TextStyle(color: p.textDim, fontSize: 13.5, height: 1.45)),
        ],
        if (m.screenshotId.isNotEmpty)
          ScreenshotThumb(botId: widget.botId, screenshotId: m.screenshotId),
        if (_error != null) Notice(_error!),
        if (widget.live) ...[
          const SizedBox(height: 2),
          PillButton(label: 'Allow once', busy: _busy == 'once', onPressed: () => _answer('once')),
          // Side by side where both labels fit; stacked on a narrow phone or large text,
          // so "Always allow" is never cut short.
          LayoutBuilder(
            builder: (context, box) {
              final always = PillButton(
                label: 'Always allow',
                kind: ButtonKind.secondary,
                busy: _busy == 'always',
                onPressed: () => _answer('always'),
              );
              final deny = PillButton(
                label: 'Deny',
                kind: ButtonKind.danger,
                busy: _busy == 'deny',
                onPressed: () => _answer('deny'),
              );
              final roomy = box.maxWidth >= MediaQuery.textScalerOf(context).scale(300);
              return roomy
                  ? Row(
                      spacing: 8,
                      children: [
                        Expanded(child: always),
                        Expanded(child: deny),
                      ],
                    )
                  : Column(spacing: 8, children: [always, deny]);
            },
          ),
        ],
      ],
    );
  }
}

const _title = {
  'sign_in': 'Sign in to',
  'sign_up': 'Create an account on',
  'verify': 'Verification code for',
};
const _submitLabel = {'sign_in': 'Sign in', 'sign_up': 'Create account', 'verify': 'Verify'};
const _secret = {'password', 'new_password', 'confirm_password'};

/// How each kind is typed, so the phone's password manager and one-time-code fill work.
({TextInputType type, Iterable<String> hints, bool obscure}) _inputFor(
  String kind,
) => switch (kind) {
  'email' => (
    type: TextInputType.emailAddress,
    hints: const [AutofillHints.username, AutofillHints.email],
    obscure: false,
  ),
  'username' => (type: TextInputType.text, hints: const [AutofillHints.username], obscure: false),
  'phone' => (
    type: TextInputType.phone,
    hints: const [AutofillHints.telephoneNumber],
    obscure: false,
  ),
  'password' => (
    type: TextInputType.visiblePassword,
    hints: const [AutofillHints.password],
    obscure: true,
  ),
  'new_password' || 'confirm_password' => (
    type: TextInputType.visiblePassword,
    hints: const [AutofillHints.newPassword],
    obscure: true,
  ),
  'otp' => (type: TextInputType.number, hints: const [AutofillHints.oneTimeCode], obscure: false),
  'name' => (type: TextInputType.name, hints: const [AutofillHints.name], obscure: false),
  _ => (type: TextInputType.text, hints: const <String>[], obscure: false),
};

/// The bot reached a sign-in, sign-up or code page and needs the person. What is typed
/// here goes in one request to the server's vault and from there into the bot's
/// browser — the bot is told the form was filled, never with what. The fields are
/// cleared as soon as it is sent.
class CredentialCard extends StatefulWidget {
  const CredentialCard({
    super.key,
    required this.botId,
    required this.botName,
    required this.message,
    required this.live,
    required this.onDone,
  });
  final String botId;
  final String botName;
  final BotMessage message;
  final bool live;
  final VoidCallback onDone;

  @override
  State<CredentialCard> createState() => _CredentialCardState();
}

class _CredentialCardState extends State<CredentialCard> {
  late final Map<String, TextEditingController> _fields = {
    for (final f in widget.message.fields) f.key: TextEditingController(),
  };
  late final Map<String, FocusNode> _focus = {
    for (final f in widget.message.fields) f.key: FocusNode(),
  };
  bool _save = true;
  String? _busy;
  String? _error;

  @override
  void dispose() {
    for (final c in _fields.values) {
      c.dispose();
    }
    for (final f in _focus.values) {
      f.dispose();
    }
    super.dispose();
  }

  Future<void> _finish(String which, Future<void> Function() call) async {
    setState(() {
      _busy = which;
      _error = null;
    });
    try {
      await call();
      for (final c in _fields.values) {
        c.clear();
      }
      widget.onDone();
    } on ApiError catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  void _submit() {
    final api = SessionScope.read(context).api;
    final m = widget.message;
    final keepable = m.fields.any((f) => _secret.contains(f.kind));
    final values = {for (final f in m.fields) f.key: _fields[f.key]!.text};
    _finish(
      'submit',
      () => api.submitCredentials(widget.botId, m.credentialRequestId, values, keepable && _save),
    );
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final api = SessionScope.of(context).api;
    final m = widget.message;
    final fields = m.fields;
    final keepable = fields.any((f) => _secret.contains(f.kind));

    return _Card(
      live: widget.live,
      children: [
        Row(
          children: [
            Icon(Icons.lock, size: 17, color: p.text),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                '${_title[m.purpose] ?? 'Sign in to'} ${m.host}',
                style: TextStyle(color: p.text, fontSize: 16, fontWeight: FontWeight.w600),
              ),
            ),
          ],
        ),
        if (m.content.isNotEmpty)
          Text(
            '${widget.botName}: ${m.content}',
            style: TextStyle(color: p.textDim, fontSize: 13.5, height: 1.45),
          ),
        if (m.screenshotId.isNotEmpty)
          ScreenshotThumb(botId: widget.botId, screenshotId: m.screenshotId),
        if (widget.live && m.retry)
          const Notice('The last attempt didn’t get past this form. The details may be wrong.'),
        if (!widget.live)
          Text('Answered', style: TextStyle(color: p.textFaint, fontSize: 13.5))
        else ...[
          for (final s in m.savedLogins)
            _SavedLogin(
              account: s.label,
              busy: _busy == s.id,
              onTap: () => _finish(
                s.id,
                () => api.chooseSavedLogin(widget.botId, m.credentialRequestId, s.id),
              ),
            ),
          if (m.savedLogins.isNotEmpty)
            Center(
              child: Text('or enter details', style: TextStyle(color: p.textFaint, fontSize: 13)),
            ),
          AutofillGroup(
            child: Column(
              spacing: 10,
              children: [
                for (var i = 0; i < fields.length; i++)
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    spacing: 6,
                    children: [
                      Text(fields[i].label, style: TextStyle(color: p.textDim, fontSize: 13.5)),
                      TextField(
                        controller: _fields[fields[i].key],
                        focusNode: _focus[fields[i].key],
                        keyboardType: _inputFor(fields[i].kind).type,
                        autofillHints: _inputFor(fields[i].kind).hints,
                        obscureText: _inputFor(fields[i].kind).obscure,
                        autocorrect: false,
                        enableSuggestions: false,
                        textInputAction: i == fields.length - 1
                            ? TextInputAction.go
                            : TextInputAction.next,
                        onSubmitted: (_) => i == fields.length - 1
                            ? _submit()
                            : _focus[fields[i + 1].key]!.requestFocus(),
                        style: TextStyle(color: p.text, fontSize: 16),
                        decoration: fieldDecoration(context),
                      ),
                    ],
                  ),
              ],
            ),
          ),
          if (keepable)
            // One control for screen readers: the sentence is the switch's label.
            MergeSemantics(
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      'Save this login so ${widget.botName} can sign in again without asking',
                      style: TextStyle(color: p.textDim, fontSize: 13.5, height: 1.4),
                    ),
                  ),
                  Switch(value: _save, onChanged: (v) => setState(() => _save = v)),
                ],
              ),
            ),
          if (_error != null) Notice(_error!),
          PillButton(
            label: _submitLabel[m.purpose] ?? 'Sign in',
            busy: _busy == 'submit',
            onPressed: _submit,
          ),
          PillButton(
            label: 'Not now',
            kind: ButtonKind.ghost,
            busy: _busy == 'cancel',
            onPressed: () =>
                _finish('cancel', () => api.cancelCredentials(widget.botId, m.credentialRequestId)),
          ),
          Text(
            'Sent to your Orgbots server’s encrypted vault. ${widget.botName} never sees what you type.',
            textAlign: TextAlign.center,
            style: TextStyle(color: p.textFaint, fontSize: 11.5, height: 1.4),
          ),
        ],
      ],
    );
  }
}

/// A saved login to use instead of typing: the account on its own line, in full, since
/// an email address rarely fits beside "Use saved login" on a phone.
class _SavedLogin extends StatelessWidget {
  const _SavedLogin({required this.account, required this.busy, required this.onTap});
  final String account;
  final bool busy;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Material(
      color: p.raised,
      borderRadius: BorderRadius.circular(Radii.md),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: busy
            ? null
            : () {
                tap();
                onTap();
              },
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            spacing: 12,
            children: [
              Icon(Icons.key_outlined, size: 20, color: p.text),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  spacing: 2,
                  children: [
                    Text(
                      'Use saved login',
                      style: TextStyle(color: p.text, fontSize: 15, fontWeight: FontWeight.w600),
                    ),
                    Text(account, style: TextStyle(color: p.textDim, fontSize: 13.5)),
                  ],
                ),
              ),
              if (busy)
                SizedBox.square(
                  dimension: 18,
                  child: CircularProgressIndicator(strokeWidth: 2, color: p.textDim),
                )
              else
                Icon(Icons.chevron_right, color: p.textFaint),
            ],
          ),
        ),
      ),
    );
  }
}
