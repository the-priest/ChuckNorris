//! Chuck's look: the window's dark-purple palette, kept in step with the
//! Python app so the two feel like one product.

pub const CSS: &str = r#"
@define-color bg        #0C0C10;
@define-color side      #09090C;
@define-color surface   #14141A;
@define-color surface2  #1B1B23;
@define-color surface3  #23232D;
@define-color line      #23232C;
@define-color line2     #30303C;
@define-color term      #08080B;
@define-color accent    #8B5CF6;
@define-color accent_hi #A78BFA;
@define-color accent_bg rgba(139,92,246,0.14);
@define-color ink       #EDEDF3;
@define-color body      #CFCFD9;
@define-color mute      #8E8E9E;
@define-color faint     #5F5F6E;
@define-color good      #4ADE9A;
@define-color bad       #F87171;
@define-color warn      #F5B454;

@define-color window_bg_color    #0C0C10;
@define-color window_fg_color    #E6E6EE;
@define-color view_bg_color      #0C0C10;
@define-color view_fg_color      #E6E6EE;
@define-color headerbar_bg_color #0C0C10;
@define-color headerbar_fg_color #E6E6EE;
@define-color headerbar_border_color #23232C;
@define-color card_bg_color      #14141A;
@define-color card_fg_color      #E6E6EE;
@define-color dialog_bg_color    #111116;
@define-color dialog_fg_color    #E6E6EE;
@define-color popover_bg_color   #16161D;
@define-color popover_fg_color   #E6E6EE;
@define-color sidebar_bg_color   #09090C;
@define-color accent_bg_color    #7C4DFF;
@define-color accent_fg_color    #FFFFFF;
@define-color accent_color       #A78BFA;

window, .main-window { background-color: @bg; color: @body;
    font-family: "Inter", "Cantarell", system-ui, sans-serif; }
headerbar { background-color: @bg; border-bottom: 1px solid @line; box-shadow: none; min-height: 48px; }

.mark    { background-color: #17151F; border: 1px solid alpha(@accent, 0.55); border-radius: 8px;
           color: @accent_hi; font-weight: 800; font-size: 12px; min-width: 22px; min-height: 22px; }
.mark-lg { border-radius: 18px; font-size: 28px; min-width: 64px; min-height: 64px; background-color: #14121C;
           border-color: alpha(@accent, 0.45);
           box-shadow: 0 0 0 6px alpha(@accent, 0.05), 0 18px 60px rgba(124,77,255,0.16); }
.title   { font-weight: 700; color: @ink; font-size: 14px; letter-spacing: 0.2px; }
.sub     { color: @faint; font-size: 11px; }
.hero    { font-weight: 800; color: @ink; font-size: 24px; }
.lede    { color: @mute; font-size: 13.5px; }

.headerbtn { background: transparent; border-radius: 8px; min-width: 32px; min-height: 32px; color: @mute;
             box-shadow: none; border: none; }
.headerbtn:hover { background-color: @surface2; color: @ink; }
.headerbtn:checked { background: transparent; color: @accent_hi; }
.meter { background-color: @surface; border: 1px solid @line; border-radius: 999px; padding: 3px 12px;
         color: @body; font-size: 11.5px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.meter-cost { color: @accent_hi; font-weight: 700; }
.pill { background-color: @surface; border: 1px solid @line; border-radius: 999px; padding: 6px 14px;
        color: @body; box-shadow: none; }
.pill:hover { border-color: alpha(@accent, 0.5); background-color: @surface2; color: @ink; }

.chat-scroll, .chat-scroll > viewport { background-color: @bg; }
.user-row { margin-top: 14px; }
.bot-row  { margin-top: 18px; }
.user-bubble { background-color: @surface2; border: 1px solid @line2; border-radius: 16px 16px 4px 16px;
               padding: 10px 14px; }
.user-bubble label { color: @ink; }
.prose  { color: @body; }
.prose link { color: @accent_hi; }
.modes button { padding: 4px 14px; font-weight: 600; }
.modes button:checked { background-color: @accent_bg; color: @accent_hi; }
.byline { color: @ink; font-size: 12.5px; font-weight: 700; }
.byline-meta { color: @faint; font-size: 11px; }
.avatar { background-color: #17151F; border: 1px solid alpha(@accent, 0.5); border-radius: 7px; color: @accent_hi;
          font-size: 11px; font-weight: 800; min-width: 20px; min-height: 20px; }
.lane   { margin-left: 9px; border-left: 2px solid @line2; padding: 6px 0 6px 21px; }
.act    { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 11.5px; color: @mute; }
.act-who  { color: @faint; }
.act-ok   { color: @good; }
.act-bad  { color: @bad; }
.act-task { color: @accent_hi; }
.act-warn { color: @warn; }
.thinking { color: @faint; font-size: 12px; font-style: italic; }

.code-block { background-color: @term; border: 1px solid @line; border-radius: 10px; }
.code-body  { font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace; font-size: 12.5px;
              color: #DCDCF0; padding: 10px 12px; }

.card      { background-color: @surface; border: 1px solid @line; border-radius: 12px; }
.card-head { padding: 7px 12px; border-bottom: 1px solid @line; }
.card-kind { color: @mute; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }
.card-body { padding: 10px 13px; }
.card-foot { padding: 0 13px 11px 13px; }
.ask-card  { border-color: alpha(@accent, 0.45); }
.ask-card .card-kind { color: @accent_hi; }
.crit-card { border-color: alpha(@bad, 0.55); }
.crit-card .card-kind { color: @bad; }
.done-card { border-color: alpha(@good, 0.45); }
.done-card .card-kind { color: @good; }
.gap-card  { border-color: alpha(@warn, 0.5); }
.gap-card .card-kind { color: @warn; }
.cmd-text  { font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace; color: #E2E2F2; font-size: 12.5px; }

.composer { background-color: @surface; border: 1px solid @line2; border-radius: 14px; padding: 8px 10px; }
.composer textview, .composer text { background: transparent; color: @ink; font-size: 14px; }
.sendbtn  { border-radius: 10px; min-width: 36px; min-height: 36px; }
.statusbar { color: @faint; font-size: 11px; }

.side { background-color: @side; border-left: 1px solid @line; }
.side-head { color: @accent_hi; font-size: 10.5px; font-weight: 700; letter-spacing: 1.2px; margin: 14px 14px 6px 14px; }
.fleet-row { padding: 6px 14px; }
.fleet-name { color: @ink; font-size: 12.5px; font-weight: 600; }
.fleet-meta { color: @faint; font-size: 11px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.dot { min-width: 8px; min-height: 8px; border-radius: 99px; background-color: @faint; }
.dot-run  { background-color: @accent_hi; }
.dot-wait { background-color: @warn; }
.dot-done { background-color: @good; }
.dot-fail { background-color: @bad; }
.crit-row { padding: 6px 14px; }
.crit-id  { color: @accent_hi; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 11.5px; font-weight: 700; }
.crit-text { color: @body; font-size: 12px; }
.chip { background-color: @surface2; border: 1px solid @line2; border-radius: 6px; padding: 1px 6px;
        color: @mute; font-size: 10.5px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.chip-ok  { color: @good; border-color: alpha(@good, 0.35); }
.chip-bad { color: @bad; border-color: alpha(@bad, 0.35); }
.phase { color: @ink; font-weight: 700; font-size: 12.5px; margin: 0 14px; }
.phase-incomplete { color: @warn; }
.unmet { color: @mute; font-size: 11.5px; margin: 2px 14px; }

/* ── the right panel: sections you open ─────────────────────────────── */
.side { background-color: @side; border-left: 1px solid @line; padding-bottom: 18px; }
.sec { border-bottom: 1px solid @line; }
.sec-head { padding: 11px 14px; border-radius: 0; min-height: 0; }
.sec-head:hover { background-color: @surface; }
.sec-chevron { color: @faint; -gtk-icon-size: 12px; }
.sec-title { color: @accent_hi; font-size: 10.5px; font-weight: 800; letter-spacing: 1.2px; }
.sec-summary { color: @mute; font-size: 11.5px; }
.sec-strip { padding: 0 14px 11px 34px; }
.sec-body { padding: 0 10px 12px 14px; }
.plan-bar trough { min-height: 5px; border-radius: 99px; background-color: @surface3; }
.plan-bar progress { min-height: 5px; border-radius: 99px; background-color: @accent; }
.plan-now { color: @ink; font-size: 12px; }
.plan-step { padding: 3px 4px 3px 20px; }
.step-mark { font-size: 12px; min-width: 14px; }
.step-text { font-size: 12.5px; }
.step-done { color: @faint; }
.inbox-new { color: @ink; font-weight: 600; font-size: 12.5px; }
.badge-new { color: @accent_hi; font-weight: 600; }
.research-ok { color: @accent_hi; -gtk-icon-size: 13px; }
.research-fail { color: @warn; -gtk-icon-size: 13px; }
.research-head { font-size: 12.5px; }
.research-head link { color: @accent_hi; }
.step-done.step-mark { color: @good; }
.step-now { color: @ink; font-weight: 700; }
.step-now.step-mark { color: @accent_hi; }
.step-todo { color: @mute; }
expander.panel-row, .panel-row { padding: 4px 4px; border-radius: 8px; }
expander.panel-row > title { padding: 4px 6px; border-radius: 8px; }
expander.panel-row > title:hover { background-color: @surface; }
expander.panel-row > title > arrow { color: @faint; min-width: 12px; }
.panel-detail { padding: 6px 4px 8px 22px; }
.panel-dim  { color: @faint; font-size: 11.5px; padding: 4px 6px; }
.panel-meta { color: @faint; font-size: 11px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.panel-text { color: @body; font-size: 12px; }
.panel-warn { color: @warn; font-size: 11.5px; }
.panel-sub  { color: @faint; font-size: 10px; font-weight: 700; letter-spacing: 1px; margin: 8px 6px 2px 6px; }
.panel-mono { color: #BDBDD0; font-size: 11px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.task-id  { color: @accent_hi; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 11.5px; font-weight: 700; }
.task-title { color: @ink; font-size: 12.5px; }
.agent-name { color: @ink; font-size: 12.5px; font-weight: 600; }
.badge { border-radius: 6px; padding: 1px 7px; font-size: 10.5px; font-weight: 700;
         font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; background-color: @surface2; color: @mute; }
.badge-running { color: @accent_hi; background-color: @accent_bg; }
.badge-review  { color: @warn; background-color: alpha(@warn, 0.12); }
.badge-queued  { color: @mute; }
.badge-merged  { color: @good; background-color: alpha(@good, 0.12); }
.badge-failed  { color: @bad; background-color: alpha(@bad, 0.12); }
.cmd-live { color: @accent_hi; font-size: 11.5px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.cmd-done { color: @body; font-size: 11.5px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.gate-phase { color: @ink; font-weight: 700; font-size: 12.5px; padding: 2px 6px 6px 6px; }
.phase-done { color: @good; }
.phase-incomplete { color: @warn; }
.pill-sm { background-color: @surface; border: 1px solid @line2; border-radius: 999px; padding: 2px 10px;
           min-height: 0; color: @body; font-size: 11px; box-shadow: none; }
.pill-sm:hover { border-color: alpha(@accent, 0.5); color: @ink; }

/* ── chat details ───────────────────────────────────────────────────── */
.jump-btn { background-color: @surface2; border: 1px solid alpha(@accent, 0.5); border-radius: 999px; color: @ink;
            padding: 5px 14px; font-size: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.45); }
.jump-btn:hover { background-color: @surface3; }
.steer-note { color: @faint; font-size: 10.5px; }
.steer-bubble { border-style: dashed; border-color: alpha(@accent, 0.45); }
expander.lane-older > title { color: @faint; font-size: 11px; }
expander.lane-older > title:hover { color: @mute; }
.composer-hint { color: @faint; font-size: 14px; }
"#;
