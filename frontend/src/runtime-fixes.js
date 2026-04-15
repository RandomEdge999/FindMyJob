(function () {
  function renderFallback() {
    var root = document.getElementById('root')
    if (!root) {
      return
    }
    root.innerHTML =
      '<div class="page-shell"><section class="panel section-panel"><div class="section-head"><div><div class="eyebrow">Frontend Error</div><h2>Something went wrong</h2><p class="section-copy">Reload the page to recover the operator console.</p></div></div></section></div>'
  }

  var fallbackRendered = false

  function showFallback() {
    if (fallbackRendered) {
      return
    }
    fallbackRendered = true
    try {
      renderFallback()
    } catch {}
  }

  window.addEventListener('error', function (event) {
    if (event && event.error) {
      showFallback()
    }
  })

  window.addEventListener('unhandledrejection', function () {
    showFallback()
  })

  if (typeof window.fetch === 'function') {
    var nativeFetch = window.fetch.bind(window)
    window.fetch = function (input, init) {
      var options = init || {}
      var timeoutMs = typeof options.timeoutMs === 'number' && options.timeoutMs > 0 ? options.timeoutMs : 30000
      var timeoutSeconds = Math.round(timeoutMs / 1000)
      var upstreamSignal = options.signal
      var controller = new AbortController()
      var timedOut = false
      var onAbort = function () {
        controller.abort(upstreamSignal && upstreamSignal.reason)
      }
      if (upstreamSignal) {
        if (upstreamSignal.aborted) {
          onAbort()
        } else {
          upstreamSignal.addEventListener('abort', onAbort, { once: true })
        }
      }
      var timeoutId = window.setTimeout(function () {
        timedOut = true
        controller.abort()
      }, timeoutMs)
      var fetchOptions = Object.assign({}, options, { signal: controller.signal })
      delete fetchOptions.timeoutMs
      return nativeFetch(input, fetchOptions)
        .catch(function (error) {
          if (
            timedOut ||
            (error instanceof Error &&
              error.name === 'AbortError' &&
              !(upstreamSignal && upstreamSignal.aborted))
          ) {
            throw new Error('Request timed out after ' + timeoutSeconds + ' seconds')
          }
          throw error
        })
        .finally(function () {
          window.clearTimeout(timeoutId)
          if (upstreamSignal) {
            upstreamSignal.removeEventListener('abort', onAbort)
          }
        })
    }
  }

  if (typeof window.EventSource === 'function') {
    var NativeEventSource = window.EventSource

    function ManagedEventSource(url, config) {
      var source = new NativeEventSource(url, config)
      var listeners = []
      var nativeAddEventListener = source.addEventListener.bind(source)
      var nativeRemoveEventListener = source.removeEventListener.bind(source)
      var nativeClose = source.close.bind(source)

      source.addEventListener = function (type, listener, options) {
        listeners.push([type, listener, options])
        return nativeAddEventListener(type, listener, options)
      }

      source.removeEventListener = function (type, listener, options) {
        for (var index = listeners.length - 1; index >= 0; index -= 1) {
          var entry = listeners[index]
          if (entry[0] === type && entry[1] === listener) {
            listeners.splice(index, 1)
          }
        }
        return nativeRemoveEventListener(type, listener, options)
      }

      source.close = function () {
        for (var index = 0; index < listeners.length; index += 1) {
          var entry = listeners[index]
          try {
            nativeRemoveEventListener(entry[0], entry[1], entry[2])
          } catch {}
        }
        listeners.length = 0
        source.onopen = null
        source.onerror = null
        source.onmessage = null
        return nativeClose()
      }

      return source
    }

    ManagedEventSource.prototype = NativeEventSource.prototype
    ManagedEventSource.CONNECTING = NativeEventSource.CONNECTING
    ManagedEventSource.OPEN = NativeEventSource.OPEN
    ManagedEventSource.CLOSED = NativeEventSource.CLOSED
    window.EventSource = ManagedEventSource
  }
})()
