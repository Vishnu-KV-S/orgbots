import 'dart:convert';
import 'dart:js_interop';
import 'dart:ui_web' as ui_web;

import 'package:flutter/widgets.dart';
import 'package:web/web.dart' as web;

/// The 3D page in an iframe, for the web build (a preview of the app in a browser).
class BotHost {
  BotHost({required void Function(Map<String, dynamic>) onMessage}) {
    _frame = web.HTMLIFrameElement()
      ..src = 'assets/assets/bot3d/index.html'
      ..style.border = 'none'
      ..style.width = '100%'
      ..style.height = '100%'
      ..style.pointerEvents = 'none'
      ..style.background = 'transparent'
      ..setAttribute('allowtransparency', 'true');
    _viewType = 'bot3d-${_next++}';
    ui_web.platformViewRegistry.registerViewFactory(_viewType, (int _) => _frame);
    _listener = ((web.MessageEvent event) {
      if (event.source != _frame.contentWindow) return;
      final data = event.data;
      if (!data.isA<JSString>()) return;
      try {
        onMessage(jsonDecode((data as JSString).toDart) as Map<String, dynamic>);
      } catch (_) {
        // Not ours.
      }
    }).toJS;
    web.window.addEventListener('message', _listener);
  }

  static int _next = 0;
  late final web.HTMLIFrameElement _frame;
  late final String _viewType;
  late final JSFunction _listener;

  void call(String method, Map<String, dynamic> arg) {
    _frame.contentWindow?.postMessage(jsonEncode({'call': method, 'arg': arg}).toJS, '*'.toJS);
  }

  Widget view() => IgnorePointer(child: HtmlElementView(viewType: _viewType));

  void dispose() => web.window.removeEventListener('message', _listener);
}
