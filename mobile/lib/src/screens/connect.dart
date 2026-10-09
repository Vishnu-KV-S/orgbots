import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import '../bot3d/bot_view.dart';
import '../bot3d/snapshots.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// First run: which Orgbots server is this phone's. The address is the one people
/// open in a browser — the app reaches the API through that same web server.
class ConnectScreen extends StatefulWidget {
  const ConnectScreen({super.key});

  @override
  State<ConnectScreen> createState() => _ConnectScreenState();
}

class _ConnectScreenState extends State<ConnectScreen> {
  late final _address = TextEditingController(
    text: SessionScope.read(context).server.replaceFirst('https://', ''),
  );
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _address.dispose();
    super.dispose();
  }

  Future<void> _go() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await SessionScope.read(context).connect(_address.text);
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final dark = p.brightness == Brightness.dark;
    return Scaffold(
      body: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: const Alignment(0, 0.4),
            colors: dark
                ? const [Color(0xFF1A1408), Colors.black]
                : const [Color(0xFFFFF4E0), Colors.white],
          ),
        ),
        child: SafeArea(
          child: LayoutBuilder(
            builder: (context, box) => SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: box.maxHeight - 48),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Column(
                      children: [
                        const SizedBox(height: 16),
                        Row(
                          mainAxisAlignment: MainAxisAlignment.center,
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            BotAvatar(
                              botId: 'cubey',
                              appearance: presets['Cubey']!,
                              size: 74,
                              mood: 'happy',
                            ),
                            Bot3D(
                              botId: 'orbit',
                              appearance: presets['Orbit']!,
                              mood: 'idle',
                              size: 150,
                            ),
                            BotAvatar(
                              botId: 'beacon',
                              appearance: presets['Beacon']!,
                              size: 74,
                              mood: 'idle',
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        Text(
                          'Orgbots',
                          style: TextStyle(
                            color: p.text,
                            fontSize: 40,
                            fontWeight: FontWeight.w800,
                            letterSpacing: -1,
                          ),
                        ),
                        const SizedBox(height: 10),
                        Text(
                          'Your AI employees, in your pocket.\nConnect to the Orgbots server your team runs.',
                          textAlign: TextAlign.center,
                          style: TextStyle(color: p.textDim, fontSize: 16, height: 1.45),
                        ),
                      ],
                    ),
                    const SizedBox(height: 32),
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      spacing: 12,
                      children: [
                        Padding(
                          padding: const EdgeInsets.only(left: 6),
                          child: Text(
                            'Server address',
                            style: TextStyle(color: p.textDim, fontSize: 13.5),
                          ),
                        ),
                        TextField(
                          controller: _address,
                          keyboardType: TextInputType.url,
                          autocorrect: false,
                          textInputAction: TextInputAction.go,
                          onSubmitted: (_) => _go(),
                          onChanged: (_) => setState(() {}),
                          style: TextStyle(color: p.text, fontSize: 16),
                          decoration: fieldDecoration(
                            context,
                            hint: 'bots.example.com',
                            pill: true,
                          ),
                        ),
                        if (_error != null) Notice(_error!),
                        PillButton(
                          label: 'Connect',
                          busy: _busy,
                          onPressed: _address.text.trim().isEmpty ? null : _go,
                        ),
                        Text(
                          'The same address you open in a browser. HTTPS is assumed; type http:// for a server on your own network.',
                          textAlign: TextAlign.center,
                          style: TextStyle(color: p.textFaint, fontSize: 13, height: 1.4),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
