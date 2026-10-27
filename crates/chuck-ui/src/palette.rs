//! The command palette (Ctrl+P): every action Chuck has, found by typing.
//! A title that starts with what you typed beats one that contains it,
//! which beats one whose words you typed out of order, which beats one
//! whose letters merely appear in order ("nwc" finds "New conversation").
//! The matching is a port of the Python app's palette.py.

use adw::prelude::*;
use gtk::{gdk, glib};

/// One action.
pub struct Item {
    pub title: String,
    pub group: &'static str,
    pub keywords: &'static str,
    pub shortcut: &'static str,
    pub run: Box<dyn Fn()>,
}

pub const GROUPS: &[&str] = &["Chat", "Commands", "Folder", "Voice", "View", "Settings"];

/// How well `query` matches: 0 (not at all) to about 1000.
pub fn score(query: &str, title: &str, group: &str, keywords: &str) -> i64 {
    let q = query.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase();
    if q.is_empty() {
        return 1;
    }
    let title = title.to_lowercase();
    let hay = format!("{title} {} {}", group.to_lowercase(), keywords.to_lowercase());
    let tl = title.chars().count() as i64;
    if title.starts_with(&q) {
        return 1000 - tl;
    }
    let words: Vec<&str> = title.split(|c: char| !(c.is_ascii_alphanumeric() || c == '/')).filter(|w| !w.is_empty()).collect();
    if words.iter().any(|w| w.starts_with(&q)) {
        return 900 - tl;
    }
    if let Some(i) = title.find(&q) {
        return 800 - title[..i].chars().count() as i64 - tl / 4;
    }
    let hl = hay.chars().count() as i64;
    if hay.contains(&q) {
        return 700 - hl / 8;
    }
    let parts: Vec<&str> = q.split(' ').collect();
    if parts.len() > 1 && parts.iter().all(|p| hay.contains(p)) {
        return 600 - hl / 8;
    }
    // the letters in order, in the title (a few gaps are fine)
    let tc: Vec<char> = title.chars().collect();
    let (mut pos, mut gaps) = (0usize, 0i64);
    for ch in q.chars().filter(|c| *c != ' ') {
        let Some(j) = tc[pos..].iter().position(|c| *c == ch) else { return 0 };
        gaps += j as i64;
        pos += j + 1;
    }
    (400 - gaps * 8 - tl / 4).max(1)
}

/// Indexes of the matching items, best first; with no query, all in group order.
pub fn rank(items: &[(String, &str, &str)], query: &str) -> Vec<usize> {
    if query.trim().is_empty() {
        let mut all: Vec<usize> = (0..items.len()).collect();
        all.sort_by_key(|&i| (GROUPS.iter().position(|g| *g == items[i].1).unwrap_or(GROUPS.len()), i));
        return all;
    }
    let mut hits: Vec<(i64, usize)> = items.iter().enumerate()
        .map(|(i, (t, g, k))| (score(query, t, g, k), i)).filter(|(s, _)| *s > 0).collect();
    hits.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    hits.into_iter().map(|(_, i)| i).collect()
}

/// Show the palette over `parent`.
pub fn show(parent: &impl IsA<gtk::Widget>, items: Vec<Item>, query: &str) {
    let dialog = adw::Dialog::builder().title("Commands").content_width(560).content_height(460).build();
    let col = gtk::Box::new(gtk::Orientation::Vertical, 8);
    col.set_margin_top(12);
    col.set_margin_bottom(12);
    col.set_margin_start(12);
    col.set_margin_end(12);
    let entry = gtk::SearchEntry::builder().placeholder_text("Type a command…").hexpand(true).build();
    col.append(&entry);
    let list = gtk::ListBox::builder().selection_mode(gtk::SelectionMode::Browse).build();
    list.add_css_class("palette-list");
    let scroll = gtk::ScrolledWindow::builder().vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).child(&list).build();
    col.append(&scroll);
    let toolbar = adw::ToolbarView::new();
    toolbar.add_top_bar(&adw::HeaderBar::new());
    toolbar.set_content(Some(&col));
    dialog.set_child(Some(&toolbar));
    let items = std::rc::Rc::new(items);
    let keys: Vec<(String, &str, &str)> = items.iter().map(|i| (i.title.clone(), i.group, i.keywords)).collect();
    let shown = std::rc::Rc::new(std::cell::RefCell::new(Vec::<usize>::new()));
    let fill = {
        let (list, items, shown) = (list.clone(), items.clone(), shown.clone());
        move |q: &str| {
            while let Some(c) = list.first_child() {
                list.remove(&c);
            }
            let order = rank(&keys, q);
            for &i in &order {
                let it = &items[i];
                let row = gtk::Box::new(gtk::Orientation::Horizontal, 10);
                row.set_margin_top(6);
                row.set_margin_bottom(6);
                row.set_margin_start(8);
                row.set_margin_end(8);
                let t = gtk::Label::builder().label(&it.title).xalign(0.0).hexpand(true).build();
                row.append(&t);
                let g = gtk::Label::new(Some(if it.shortcut.is_empty() { it.group } else { it.shortcut }));
                g.add_css_class("panel-dim");
                row.append(&g);
                list.append(&row);
            }
            if let Some(first) = list.row_at_index(0) {
                list.select_row(Some(&first));
            }
            *shown.borrow_mut() = order;
        }
    };
    fill("");
    let go = {
        let (items, shown, dialog) = (items.clone(), shown.clone(), dialog.clone());
        move |idx: usize| {
            let Some(&i) = shown.borrow().get(idx) else { return };
            dialog.close();
            let items = items.clone();
            // after the dialog is gone: an action may open another one
            glib::idle_add_local_once(move || (items[i].run)());
        }
    };
    let f2 = fill.clone();
    entry.connect_search_changed(move |e| f2(&e.text()));
    let (g1, l1) = (go.clone(), list.clone());
    entry.connect_activate(move |_| g1(l1.selected_row().map_or(0, |r| r.index().max(0) as usize)));
    let g2 = go.clone();
    list.connect_row_activated(move |_, r| g2(r.index().max(0) as usize));
    // arrows move through the list while typing
    let keys = gtk::EventControllerKey::new();
    let l2 = list.clone();
    keys.connect_key_pressed(move |_, key, _, _| {
        let step = match key {
            gdk::Key::Down => 1,
            gdk::Key::Up => -1,
            _ => return glib::Propagation::Proceed,
        };
        let cur = l2.selected_row().map_or(-1, |r| r.index());
        if let Some(r) = l2.row_at_index((cur + step).max(0)) {
            l2.select_row(Some(&r));
            r.grab_focus();
        }
        glib::Propagation::Stop
    });
    entry.add_controller(keys);
    dialog.present(Some(parent));
    entry.set_text(query);
    entry.grab_focus();
    entry.set_position(-1);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranking_like_the_python_app() {
        let items: Vec<(String, &str, &str)> = vec![
            ("New conversation".into(), "Chat", "clear fresh"),
            ("Settings".into(), "Settings", "preferences key budget"),
            ("/doctor — check everything".into(), "Commands", "health diagnose"),
            ("Switch to Chat".into(), "Chat", "mode answer"),
            ("Undo the last job".into(), "Folder", "rewind revert"),
        ];
        assert_eq!(rank(&items, "set")[0], 1, "a title that starts with it");
        assert_eq!(rank(&items, "doc")[0], 2, "a word that starts with it");
        assert_eq!(rank(&items, "rewind")[0], 4, "keywords count");
        assert_eq!(rank(&items, "nwc")[0], 0, "letters in order");
        assert!(rank(&items, "zzz").is_empty());
        assert_eq!(rank(&items, "").len(), 5);
        assert_eq!(score("new conversation", "New conversation", "Chat", ""), 1000 - 16);
    }
}
