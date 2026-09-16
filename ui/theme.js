(function() {
    // 1. Inject CSS
    const style = document.createElement('style');
    style.innerHTML = `
        /* Dark Theme Overrides */
        body.dark-theme {
            --bg: #121212;
            --card: #1e1e1e;
            --line: #333;
            --line2: #2b2b2b;
            --soft: #282828;
            --ink: #e0e0e0;
            --dim: #a0a0a0;
            --faint: #6e6e6e;
            --accent: #3f7bf6;
            --accent2: #5c90f8;
            --ok: #20c970;
            --okbg: #123524;
            --err: #f04e42;
            --errbg: #3d1614;
            --warn: #e09d00;
            --warnbg: #332611;
            --b1: #1a2a44;
            --b2: #163623;
            --b3: #3b2716;
        }
        
        /* General Dark Mode Fixes */
        body.dark-theme select, body.dark-theme input, body.dark-theme textarea {
            background-color: var(--card);
            color: var(--ink);
            border-color: var(--line);
        }
        body.dark-theme .btn {
            background-color: var(--card);
            color: var(--ink);
        }
        body.dark-theme .btn:hover:not(:disabled) {
            background-color: var(--soft);
        }
        body.dark-theme .btn.primary {
            background-color: var(--accent);
            color: #fff;
        }
        body.dark-theme .btn.primary:hover:not(:disabled) {
            background-color: var(--accent2);
        }
        body.dark-theme .btn.danger {
            /* в светлой палитре рамка #f3c9c5 — на тёмном фоне она светится */
            border-color: #5a2a26;
            color: var(--err);
        }
        body.dark-theme .btn.danger:hover:not(:disabled) {
            background-color: var(--errbg);
            border-color: #7a3a34;
        }
        body.dark-theme .side {
            background-color: #171717;
        }
        body.dark-theme .loader-container {
            background-color: var(--card);
            color: var(--ink);
        }
        body.dark-theme .spinner {
            border-color: var(--line);
            border-top-color: var(--accent);
        }
        
        /* Scrollbar for dark theme */
        body.dark-theme ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        body.dark-theme ::-webkit-scrollbar-track {
            background: var(--bg);
        }
        body.dark-theme ::-webkit-scrollbar-thumb {
            background: var(--line);
            border-radius: 4px;
        }
        body.dark-theme ::-webkit-scrollbar-thumb:hover {
            background: var(--dim);
        }

        /* Toggle Button Styles — только иконка, без подписи */
        #theme-toggle-btn {
            background: var(--card);
            border: 1px solid var(--line);
            color: var(--ink);
            border-radius: 50%;
            width: 30px;
            height: 30px;
            padding: 0;
            font-size: 14px;
            line-height: 1;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
            margin-left: 10px;
        }
        #theme-toggle-btn:focus {
            outline: none;
        }
        #theme-toggle-btn:hover {
            background: var(--soft);
        }
        .theme-toggle-fixed {
            position: absolute;
            top: 16px;
            right: 20px;
            z-index: 9999;
        }
    `;
    document.head.appendChild(style);

    // localStorage в webview на file:// может быть недоступен — не роняем страницу.
    function readTheme() {
        try { return localStorage.getItem('hh_agent_theme'); } catch (e) { return null; }
    }
    function writeTheme(value) {
        try { localStorage.setItem('hh_agent_theme', value); } catch (e) { /* ignore */ }
    }

    let isDark = readTheme() === 'dark';

    // 2. Initialize Theme synchronously to avoid flicker.
    // document.body может ещё не существовать, если скрипт подключили в <head>.
    function applyTheme() {
        if (!document.body) return;
        document.body.classList.toggle('dark-theme', isDark);
    }
    applyTheme();

    // 3. Create Toggle Button
    const btn = document.createElement('button');
    btn.id = 'theme-toggle-btn';

    function updateBtn() {
        // Только иконка: подпись «Свет/Тьма» ничего не добавляла,
        // а место в шапке занимала.
        btn.textContent = isDark ? '🌙' : '☀️';
        var hint = isDark ? 'Тёмная тема — переключить на светлую'
                          : 'Светлая тема — переключить на тёмную';
        btn.title = hint;
        btn.setAttribute('aria-label', hint);
    }
    updateBtn();

    btn.addEventListener('click', () => {
        isDark = !isDark;
        applyTheme();
        writeTheme(isDark ? 'dark' : 'light');
        updateBtn();
    });

    // 4. Inject Button when DOM is ready
    function injectBtn() {
        applyTheme();
        if (document.getElementById('theme-toggle-btn')) return;

        const appHeader = document.querySelector('header.top .prof');
        const runHeader = document.querySelector('header .top');
        
        if (appHeader) {
            // app.html: insert before .prof
            appHeader.parentNode.insertBefore(btn, appHeader);
        } else if (runHeader) {
            // run.html: append to top header
            runHeader.appendChild(btn);
        } else {
            // wizard.html, loader.html: fixed positioning
            btn.classList.add('theme-toggle-fixed');
            document.body.appendChild(btn);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectBtn);
    } else {
        injectBtn();
    }
})();
