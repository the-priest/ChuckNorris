//! Pictures and videos Chat found: a gallery of thumbnails that open their
//! page, and video cards that open in the browser or download with yt-dlp
//! (one video, at most 1080p and 2 GB, into Downloads — and ended with
//! Chuck if it is still going when the window closes).

use crate::chat::{Picture, Video};
use gtk::prelude::*;
use gtk::{gio, glib, pango};
use std::io::{BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

fn open_uri(uri: &str, w: &impl IsA<gtk::Widget>) {
    let win = w.root().and_downcast::<gtk::Window>();
    gtk::UriLauncher::new(uri).launch(win.as_ref(), None::<&gio::Cancellable>, |_| {});
}

fn caption(text: &str) -> gtk::Label {
    let l = gtk::Label::builder().label(text).xalign(0.0).ellipsize(pango::EllipsizeMode::End).max_width_chars(28).build();
    l.add_css_class("gal-cap");
    l
}

fn thumb(path: &Path, w: i32, h: i32) -> gtk::Widget {
    if path.is_file() {
        let pic = gtk::Picture::for_filename(path);
        pic.set_content_fit(gtk::ContentFit::Cover);
        pic.set_can_shrink(true);
        pic.set_size_request(w, h);
        pic.add_css_class("gal-pic");
        pic.upcast()
    } else {
        let l = gtk::Label::new(Some("picture no longer on disk"));
        l.add_css_class("panel-dim");
        l.set_size_request(w, h);
        l.upcast()
    }
}

/// The pictures, side by side; a click opens the page each came from.
pub fn gallery(body: &gtk::Box, items: &[Picture]) {
    let flow = gtk::FlowBox::builder().selection_mode(gtk::SelectionMode::None).homogeneous(true)
        .column_spacing(10).row_spacing(10).max_children_per_line(3).min_children_per_line(3).build();
    for it in items {
        let cell = gtk::Box::new(gtk::Orientation::Vertical, 4);
        let btn = gtk::Button::new();
        btn.add_css_class("flat");
        btn.add_css_class("thumb-btn");
        btn.set_child(Some(&thumb(Path::new(&it.path), 190, 140)));
        btn.set_tooltip_text(Some(&format!("Open {}", it.page)));
        let page = it.page.clone();
        btn.connect_clicked(move |b| open_uri(&page, b));
        cell.append(&btn);
        cell.append(&caption(&if it.title.is_empty() { chuck_web::domain(&it.page) } else {
            format!("{} · {}", it.title, chuck_web::domain(&it.page)) }));
        flow.insert(&cell, -1);
    }
    body.append(&flow);
}

/// Where downloads go: the user's Downloads folder.
pub fn downloads() -> PathBuf {
    glib::user_special_dir(glib::UserDirectory::Downloads)
        .unwrap_or_else(|| glib::home_dir().join("Downloads"))
}

pub fn ytdlp() -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|p| std::env::split_paths(&p).map(|d| d.join("yt-dlp")).find(|p| p.is_file()))
}

/// Download one video with yt-dlp, on its own thread; progress lines and
/// the end come back through `tx` (Ok(path) or Err(why)).
pub fn download(url: &str, proxy: Option<String>, tx: std::sync::mpsc::Sender<Result<String, String>>, progress: std::sync::mpsc::Sender<String>) {
    let Some(bin) = ytdlp() else {
        let _ = tx.send(Err("yt-dlp isn't installed (pacman -S yt-dlp / apt install yt-dlp)".into()));
        return;
    };
    let dir = downloads();
    let url = url.to_string();
    std::thread::Builder::new().name("yt-dlp".into()).spawn(move || {
        let _ = std::fs::create_dir_all(&dir);
        let mut c = Command::new(bin);
        c.args(["--no-playlist", "--max-filesize", "2G", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--trim-filenames",
                "150", "--newline", "--print", "after_move:filepath", "-o"])
            .arg(dir.join("%(title)s.%(ext)s"));
        if let Some(p) = proxy.filter(|p| !p.trim().is_empty()) {
            c.args(["--proxy", p.trim()]);
        }
        c.arg("--").arg(&url).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        unsafe {
            c.pre_exec(|| {
                libc::setsid();
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                Ok(())
            });
        }
        let mut child = match c.spawn() {
            Ok(ch) => ch,
            Err(e) => {
                let _ = tx.send(Err(e.to_string()));
                return;
            }
        };
        let _guard = chuck_tools::Service::register(child.id() as i32);
        let err = child.stderr.take();
        let errs = std::thread::spawn(move || err.map(|e| BufReader::new(e).lines().map_while(Result::ok).collect::<Vec<_>>())
            .unwrap_or_default());
        let mut saved = None;
        if let Some(out) = child.stdout.take() {
            for l in BufReader::new(out).lines().map_while(Result::ok) {
                if l.starts_with('/') && Path::new(&l).is_file() {
                    saved = Some(l);
                } else if l.contains("[download]") && l.contains('%') {
                    let _ = progress.send(l.trim_start_matches("[download]").trim().to_string());
                }
            }
        }
        let ok = child.wait().is_ok_and(|s| s.success());
        let errs = errs.join().unwrap_or_default();
        let _ = tx.send(match (ok, saved) {
            (true, Some(p)) => Ok(p),
            (true, None) => Ok(dir.display().to_string()),
            _ => Err(errs.iter().rev().find(|l| l.contains("ERROR")).cloned()
                .unwrap_or_else(|| errs.last().cloned().unwrap_or_else(|| "yt-dlp failed".into()))),
        });
    }).ok();
}

/// One card per video: its poster, title and length, Open and Download.
pub fn videos(body: &gtk::Box, items: &[Video], proxy: Option<String>) {
    for v in items {
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        row.add_css_class("video-row");
        if let Some(t) = &v.thumb {
            row.append(&thumb(Path::new(t), 160, 90));
        }
        let col = gtk::Box::new(gtk::Orientation::Vertical, 4);
        col.set_hexpand(true);
        let title = gtk::Label::builder().label(if v.title.is_empty() { &v.url } else { &v.title }).xalign(0.0).wrap(true)
            .wrap_mode(pango::WrapMode::WordChar).build();
        title.add_css_class("cmd-text");
        col.append(&title);
        let meta = gtk::Label::builder().label(format!("{}{}", chuck_web::domain(&v.url),
                                                       v.length.as_ref().map(|l| format!(" · {l}")).unwrap_or_default()))
            .xalign(0.0).build();
        meta.add_css_class("panel-dim");
        col.append(&meta);
        let btns = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let open = gtk::Button::with_label("Open");
        open.add_css_class("pill-sm");
        let u = v.url.clone();
        open.connect_clicked(move |b| open_uri(&u, b));
        btns.append(&open);
        let dl = gtk::Button::with_label("Download");
        dl.add_css_class("pill-sm");
        let status = gtk::Label::new(None);
        status.add_css_class("panel-dim");
        status.set_ellipsize(pango::EllipsizeMode::Middle);
        if ytdlp().is_none() {
            dl.set_sensitive(false);
            dl.set_tooltip_text(Some("Needs yt-dlp (pacman -S yt-dlp / apt install yt-dlp)"));
        } else {
            dl.set_tooltip_text(Some(&format!("One video, at most 1080p and 2 GB, into {}", downloads().display())));
        }
        let (u, st, p) = (v.url.clone(), status.clone(), proxy.clone());
        dl.connect_clicked(move |b| {
            b.set_sensitive(false);
            st.set_label("starting…");
            let (tx, rx) = std::sync::mpsc::channel();
            let (ptx, prx) = std::sync::mpsc::channel();
            download(&u, p.clone(), tx, ptx);
            let (st, b) = (st.clone(), b.clone());
            glib::timeout_add_local(std::time::Duration::from_millis(250), move || {
                if let Some(last) = prx.try_iter().last() {
                    st.set_label(&last);
                }
                match rx.try_recv() {
                    Ok(Ok(path)) => {
                        st.set_label(&format!("saved: {path}"));
                        st.set_tooltip_text(Some(&path));
                        glib::ControlFlow::Break
                    }
                    Ok(Err(e)) => {
                        st.set_label(&format!("failed: {e}"));
                        b.set_sensitive(true);
                        glib::ControlFlow::Break
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
                    Err(_) => glib::ControlFlow::Break,
                }
            });
        });
        btns.append(&dl);
        btns.append(&status);
        col.append(&btns);
        row.append(&col);
        body.append(&row);
    }
}
