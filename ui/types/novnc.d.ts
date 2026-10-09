/**
 * The part of noVNC's RFB client (`@novnc/novnc`, core/rfb.js) the computer's desktop
 * view uses. The package ships no types, and the community ones trail its releases.
 */
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(
      target: HTMLElement,
      urlOrChannel: string | WebSocket,
      options?: { shared?: boolean; credentials?: { password?: string }; wsProtocols?: string[] },
    );
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    focusOnClick: boolean;
    showDotCursor: boolean;
    qualityLevel: number;
    compressionLevel: number;
    background: string;
    disconnect(): void;
    focus(): void;
    blur(): void;
    clipboardPasteFrom(text: string): void;
    sendKey(keysym: number, code: string | null, down?: boolean): void;
  }
}
