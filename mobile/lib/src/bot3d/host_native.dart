import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

/// The 3D page in a transparent, non-interactive web view.
class BotHost {
  BotHost({required void Function(Map<String, dynamic>) onMessage}) {
    controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.transparent)
      ..addJavaScriptChannel(
        'Bot3D',
        onMessageReceived: (message) {
          try {
            onMessage(jsonDecode(message.message) as Map<String, dynamic>);
          } catch (_) {
            // Not ours.
          }
        },
      )
      ..loadFlutterAsset('assets/bot3d/index.html');
  }

  late final WebViewController controller;

  /// `window.orgbots.<method>(arg)` in the page.
  void call(String method, Map<String, dynamic> arg) {
    controller
        .runJavaScript('window.orgbots && window.orgbots.$method(${jsonEncode(arg)})')
        .catchError((Object _) {});
  }

  Widget view() => IgnorePointer(child: WebViewWidget(controller: controller));

  void dispose() {}
}
