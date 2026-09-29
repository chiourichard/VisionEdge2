/* One bounded request per viewer: slow clients fetch the newest frame next. */
(function (root) {
  'use strict';
  class LatestPreview {
    constructor(image, endpoint) {
      this.image = image;
      this.endpoint = endpoint;
      this.active = false;
      this.mode = 'raw';
      this.revision = '';
      this.generation = 0;
      this.timer = null;
      this.controller = null;
      this.url = null;
      this.busy = false;
    }
    configure(active, mode) {
      if (this.active === active && this.mode === mode) return;
      this.stop();
      this.mode = mode;
      this.active = active;
      if (active) this.pump();
    }
    stop() {
      this.active = false;
      this.generation++;
      clearTimeout(this.timer);
      this.controller?.abort();
      this.image.removeAttribute('src');
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = null;
      this.revision = '';
    }
    async pump() {
      if (!this.active || this.busy) return;
      this.busy = true;
      const generation = this.generation, started = Date.now();
      const controller = new AbortController();
      this.controller = controller;
      let delay = 100, pendingUrl = null;
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const query = new URLSearchParams({mode: this.mode, after: this.revision});
        const response = await fetch(`${this.endpoint}?${query}`, {
          cache: 'no-store', signal: controller.signal
        });
        if (generation !== this.generation) return;
        if (response.status === 204) return;
        if (!response.ok) throw new Error(`Preview HTTP ${response.status}`);
        const blob = await response.blob();
        if (generation !== this.generation || controller.signal.aborted) return;
        pendingUrl = URL.createObjectURL(blob);
        // Keep the displayed frame intact until the replacement has decoded.
        const nextImage = new Image();
        nextImage.src = pendingUrl;
        // Decode completion provides backpressure too; abort also cancels this wait.
        await new Promise((resolve, reject) => {
          const abort = () => reject(new Error('Preview cancelled'));
          controller.signal.addEventListener('abort', abort, {once: true});
          nextImage.decode().then(resolve, reject).finally(() =>
            controller.signal.removeEventListener('abort', abort));
          if (controller.signal.aborted) abort();
        });
        if (generation === this.generation && !controller.signal.aborted) {
          const previousUrl = this.url;
          this.image.src = pendingUrl;
          this.url = pendingUrl;
          pendingUrl = null;
          if (previousUrl) URL.revokeObjectURL(previousUrl);
          this.revision = response.headers.get('X-Frame-Sequence') || '';
        }
      } catch (_) {
        delay = 1000;
        if (generation === this.generation) {
          this.revision = '';
          // A transient failure must not flash black between valid frames.
          // Explicit stop/visibility/status changes still clear stale imagery.
        }
      } finally {
        if (pendingUrl) URL.revokeObjectURL(pendingUrl);
        clearTimeout(timeout);
        this.controller = null;
        this.busy = false;
        if (this.active) {
          this.timer = setTimeout(() => this.pump(),
            generation === this.generation ? Math.max(0, delay - (Date.now() - started)) : 0);
        }
      }
    }
  }
  root.LatestPreview = LatestPreview;
})(globalThis);
