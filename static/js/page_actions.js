// Page actions without inline event handlers. The Content-Security-Policy
// (app.py, _security_headers) allows scripts only from this site, the two
// CDNs and <script nonce> blocks, so onclick="..." attributes no longer run,
// and neither does anything injected into a page as an attribute. Elements
// declare what they do with data-* attributes instead:
//
//   data-click="fnName"  data-args='["a", 1]'   click calls window.fnName("a", 1)
//   data-change="fnName" data-args='[...]'      change calls it; with data-pass-checked
//                                               the element's checked state is the last argument
//   data-autosubmit                             change submits the element's form (or the form itself)
//   data-confirm="Question?"                    on a <form>: asks before it is submitted
//   data-click-target="#id"                     click forwards to another element (file inputs)
(function () {
    'use strict';

    function call(el, name, extra) {
        var fn = window[name];
        if (typeof fn !== 'function') {
            console.error('Page action not found:', name);
            return undefined;
        }
        var args = [];
        try {
            args = JSON.parse(el.getAttribute('data-args') || '[]');
        } catch (err) {
            console.error('Bad data-args on', el, err);
            return undefined;
        }
        return fn.apply(el, args.concat(extra || []));
    }

    document.addEventListener('click', function (e) {
        var forward = e.target.closest('[data-click-target]');
        if (forward) {
            var target = document.querySelector(forward.getAttribute('data-click-target'));
            // the forwarded click bubbles back up through this element: ignore it
            if (target && !target.contains(e.target)) target.click();
            return;
        }
        var el = e.target.closest('[data-click]');
        if (!el || el.disabled) return;
        if (el.tagName === 'A') e.preventDefault();
        call(el, el.getAttribute('data-click'));
    });

    document.addEventListener('change', function (e) {
        var el = e.target.closest('[data-change]');
        if (el) {
            call(el, el.getAttribute('data-change'), el.hasAttribute('data-pass-checked') ? [el.checked] : []);
        }
        var auto = e.target.closest('[data-autosubmit]');
        if (auto) {
            var form = auto.tagName === 'FORM' ? auto : auto.form;
            if (form) form.submit();
        }
    });

    document.addEventListener('submit', function (e) {
        var form = e.target;
        var question = form && form.getAttribute ? form.getAttribute('data-confirm') : null;
        if (question && !window.confirm(question)) e.preventDefault();
    }, true);
})();
