function initNavToggle() {
  const toggle = document.querySelector('[data-nav-toggle]');
  if (!(toggle instanceof HTMLButtonElement)) {
    return;
  }

  const setOpen = (open) => {
    document.body.dataset.navOpen = String(open);
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  };

  toggle.addEventListener('click', () => {
    setOpen(document.body.dataset.navOpen !== 'true');
  });

  document.querySelectorAll('[data-nav-close], .nav-link').forEach((node) => {
    node.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 940px)').matches) {
        setOpen(false);
      }
    });
  });

  window.addEventListener('resize', () => {
    if (!window.matchMedia('(max-width: 940px)').matches) {
      setOpen(false);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      setOpen(false);
    }
  });
}

function initCollapsibleCards() {
  document.querySelectorAll('[data-collapsible-card]').forEach((card) => {
    if (!(card instanceof HTMLElement)) {
      return;
    }
    const toggle = card.querySelector('[data-collapse-toggle]');
    const body = card.querySelector('[data-collapse-body]');
    if (!(toggle instanceof HTMLButtonElement) || !(body instanceof HTMLElement)) {
      return;
    }

    const expandedLabel = toggle.dataset.expandedLabel || 'Collapse';
    const collapsedLabel = toggle.dataset.collapsedLabel || 'Expand';

    const setExpanded = (expanded) => {
      body.hidden = !expanded;
      card.classList.toggle('is-collapsed', !expanded);
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.textContent = expanded ? expandedLabel : collapsedLabel;
    };

    const initialExpanded = card.dataset.collapsed !== 'true' && !body.hidden;
    setExpanded(initialExpanded);

    toggle.addEventListener('click', () => {
      setExpanded(body.hidden);
    });
  });
}

document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }

  const message = form.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
    return;
  }

  const submit = form.querySelector('button[type="submit"], input[type="submit"]');
  if (!(submit instanceof HTMLButtonElement || submit instanceof HTMLInputElement)) {
    return;
  }

  submit.disabled = true;
  submit.classList.add('is-busy');
  form.setAttribute('aria-busy', 'true');

  if (submit instanceof HTMLButtonElement && form.dataset.busy) {
    submit.dataset.originalLabel = submit.textContent || '';
    submit.textContent = form.dataset.busy;
  }
});

document.addEventListener('DOMContentLoaded', () => {
  initNavToggle();
  initCollapsibleCards();
  initAutopilotStatusPoll();
});


function humanizeSlug(value) {
  return String(value || '')
    .replace(/[-_]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase()) || '-';
}

function initAutopilotStatusPoll() {
  const panel = document.querySelector('[data-autopilot-status-panel]');
  if (!(panel instanceof HTMLElement)) {
    return;
  }
  const statusUrl = panel.dataset.statusUrl;
  if (!statusUrl) {
    return;
  }

  const setField = (name, value) => {
    document.querySelectorAll(`[data-autopilot-field="${name}"]`).forEach((node) => {
      node.textContent = String(value ?? '-');
    });
  };

  const applyStatus = (payload) => {
    const runStatus = payload.run_status || {};
    setField('run_stage', humanizeSlug(runStatus.stage || 'idle'));
    setField('run_id', runStatus.run_id || (payload.latest_run || {}).run_id || 'No run yet');
    setField('blocked_applications', payload.blocked_applications ?? 0);
    setField('unresolved_prompts', payload.unresolved_prompts ?? payload.queue_depth ?? 0);
    setField('browser_mode', payload.browser_mode || runStatus.browser_mode || '-');
    const latestError = runStatus.latest_error || payload.last_failure || '';
    setField('latest_error', latestError || '-');
    const errorBlock = document.querySelector('[data-autopilot-error-block]');
    if (errorBlock instanceof HTMLElement) {
      errorBlock.hidden = !latestError;
    }
  };

  const poll = async () => {
    try {
      const response = await fetch(statusUrl, { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        return;
      }
      applyStatus(await response.json());
    } catch (_error) {
      // Keep the current UI state if polling fails.
    }
  };

  poll();
  window.setInterval(poll, 3000);
}
