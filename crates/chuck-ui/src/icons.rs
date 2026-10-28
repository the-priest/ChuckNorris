//! Chuck's own icons. Every button's icon ships with the app, so the window
//! looks the same whatever icon theme the desktop uses (a theme without a
//! symbolic icon draws an empty square). They're symbolic: GTK recolours
//! them with the text colour.

use gtk::gdk;

const ICONS: &[(&str, &str)] = &[
    ("chats", r##"<rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="#bebebe" stroke-width="1.5"/><rect x="2" y="3" width="4" height="10" fill="#bebebe"/>"##),
    ("panel", r##"<rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="#bebebe" stroke-width="1.5"/><rect x="10" y="3" width="4" height="10" fill="#bebebe"/>"##),
    ("new", r##"<path d="M7 2.75H4A1.25 1.25 0 0 0 2.75 4v8A1.25 1.25 0 0 0 4 13.25h8A1.25 1.25 0 0 0 13.25 12V9" fill="none" stroke="#bebebe" stroke-width="1.5" stroke-linecap="round"/><path d="M12.2 1.8a1.3 1.3 0 0 1 1.9 1.9L8.5 9.3 6 10l.7-2.5z" fill="#bebebe"/>"##),
    ("settings", r##"<path d="M6.6 1h2.8l.4 1.9a5.2 5.2 0 0 1 1.3.75l1.85-.6 1.4 2.4-1.45 1.3a5.3 5.3 0 0 1 0 1.5l1.45 1.3-1.4 2.4-1.85-.6a5.2 5.2 0 0 1-1.3.75L9.4 15H6.6l-.4-1.9a5.2 5.2 0 0 1-1.3-.75l-1.85.6-1.4-2.4 1.45-1.3a5.3 5.3 0 0 1 0-1.5L1.65 6.45l1.4-2.4 1.85.6a5.2 5.2 0 0 1 1.3-.75zM8 5.6A2.4 2.4 0 1 0 8 10.4 2.4 2.4 0 1 0 8 5.6z" fill="#bebebe" fill-rule="evenodd"/>"##),
    ("speaker", r##"<path d="M2 6h2.5L8 3v10L4.5 10H2z" fill="#bebebe"/><path d="M10.5 5.5a3.5 3.5 0 0 1 0 5M12.2 3.6a6 6 0 0 1 0 8.8" fill="none" stroke="#bebebe" stroke-width="1.4" stroke-linecap="round"/>"##),
    ("speaker-off", r##"<path d="M2 6h2.5L8 3v10L4.5 10H2z" fill="#bebebe"/><path d="M10.5 6l4 4m0-4l-4 4" stroke="#bebebe" stroke-width="1.4" stroke-linecap="round"/>"##),
    ("mic", r##"<rect x="5.5" y="1.5" width="5" height="8.5" rx="2.5" fill="#bebebe"/><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5M5.5 14.5h5" fill="none" stroke="#bebebe" stroke-width="1.4" stroke-linecap="round"/>"##),
    ("send", r##"<path d="M8 13.5V3M3.5 7.5L8 3l4.5 4.5" fill="none" stroke="#bebebe" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>"##),
    ("stop", r##"<rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="#bebebe"/>"##),
    ("play", r##"<path d="M4.5 2.5v11l9-5.5z" fill="#bebebe"/>"##),
    ("pause", r##"<rect x="3.5" y="2.5" width="3" height="11" rx="1" fill="#bebebe"/><rect x="9.5" y="2.5" width="3" height="11" rx="1" fill="#bebebe"/>"##),
    ("down", r##"<path d="M4 6l4 4 4-4" fill="none" stroke="#bebebe" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>"##),
    ("right", r##"<path d="M6 4l4 4-4 4" fill="none" stroke="#bebebe" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>"##),
    ("trash", r##"<path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.3a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L12 4" fill="none" stroke="#bebebe" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>"##),
    ("search", r##"<circle cx="6.8" cy="6.8" r="4.3" fill="none" stroke="#bebebe" stroke-width="1.6"/><path d="M10 10l4 4" stroke="#bebebe" stroke-width="1.8" stroke-linecap="round"/>"##),
    ("image", r##"<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.5" fill="none" stroke="#bebebe" stroke-width="1.5"/><path d="M3 12l3.5-4 2.5 2.7 1.8-1.9L13 12z" fill="#bebebe"/><circle cx="10.8" cy="5.8" r="1.2" fill="#bebebe"/>"##),
    ("video", r##"<rect x="1.5" y="3.5" width="9" height="9" rx="1.5" fill="none" stroke="#bebebe" stroke-width="1.5"/><path d="M11 7l3.5-2.2v6.4L11 9z" fill="#bebebe"/>"##),
    ("close", r##"<path d="M4 4l8 8m0-8l-8 8" fill="none" stroke="#bebebe" stroke-width="1.8" stroke-linecap="round"/>"##),
    ("page", r##"<path d="M3.75 1.75h5.5l3 3v9.5h-8.5z" fill="none" stroke="#bebebe" stroke-width="1.5" stroke-linejoin="round"/><path d="M6 8h4M6 10.5h4" stroke="#bebebe" stroke-width="1.3" stroke-linecap="round"/>"##),
];

/// Where they're written (GTK loads icons from files in a theme folder).
fn dir() -> std::path::PathBuf {
    chuck_store::ledger::data_dir().join("icons")
}

/// Write the icons (when missing or changed) and add them to the display's
/// icon theme. Call once, after GTK is up.
pub fn install() {
    let base = dir();
    let actions = base.join("hicolor/scalable/actions");
    let _ = std::fs::create_dir_all(&actions);
    for (name, body) in ICONS {
        let svg = format!(r##"<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">{body}</svg>"##);
        let p = actions.join(format!("chuck-{name}-symbolic.svg"));
        if std::fs::read_to_string(&p).ok().as_deref() != Some(svg.as_str()) {
            let _ = std::fs::write(&p, svg);
        }
    }
    if let Some(display) = gdk::Display::default() {
        gtk::IconTheme::for_display(&display).add_search_path(&base);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_icon_is_well_formed_svg() {
        for (name, body) in super::ICONS {
            let svg = format!(r#"<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>"#);
            // balanced tags: each element is self-closed
            assert_eq!(svg.matches('<').count(), svg.matches('>').count(), "{name}");
            assert!(body.matches("/>").count() >= 1, "{name}");
        }
    }
}
