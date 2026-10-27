//! Chuck himself, in the corner of the chat: when the Lead has something to
//! say (a step's news, a question, the finished job) he pops up with it in a
//! speech bubble, and his mouth moves while it's read aloud. A click on the
//! bubble sends him away; otherwise he goes by himself once it's been read.
//! Settings › Voice turns him off.

use gtk::prelude::*;
use gtk::{cairo, glib};
use std::cell::RefCell;
use std::f64::consts::PI;
use std::rc::Rc;
use std::time::{Duration, Instant};

/// The most of a message the bubble holds: the rest is in the chat.
const BUBBLE_CHARS: usize = 260;

#[derive(Clone)]
pub struct Buddy {
    pub root: gtk::Revealer,
    bubble: gtk::Label,
    face: gtk::DrawingArea,
    st: Rc<RefCell<Look>>,
}

struct Look {
    /// Talking until then (no voice: as long as reading it takes).
    talk_until: Option<Instant>,
    /// The speech whose sound moves his mouth.
    speech: Option<(chuck_voice::Voice, u64)>,
    /// He leaves at this time (None: while hovered, or not shown).
    leave_at: Option<Instant>,
    hovered: bool,
    blink_at: Instant,
    t0: Instant,
    ticking: bool,
}

/// Markdown as words: code blocks out, markers off, code kept as written.
fn plain_words(text: &str) -> String {
    let mut out = Vec::new();
    let mut fenced = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("```") || t.starts_with("~~~") {
            fenced = !fenced;
            continue;
        }
        if fenced || t.is_empty() {
            continue;
        }
        let t = t.trim_start_matches('#').trim_start_matches(['-', '*', '•', '>']).trim();
        let t = t.replace("**", "").replace("__", "").replace('`', "");
        // lines run on as sentences: one that ends without a stop gets one
        out.push(if t.ends_with(['.', '!', '?', ':', ';', ',']) { t } else { format!("{t}.") });
    }
    out.join(" ").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// What a message comes to in a bubble: plain words, its first lines.
pub fn gist(text: &str) -> String {
    let plain = plain_words(text);
    if plain.chars().count() <= BUBBLE_CHARS {
        return plain;
    }
    // whole sentences that fit, else a clean cut at a word
    let mut out = String::new();
    for s in plain.split_inclusive(['.', '!', '?']) {
        if out.chars().count() + s.chars().count() > BUBBLE_CHARS {
            break;
        }
        out.push_str(s);
    }
    if out.trim().is_empty() {
        let cut: String = plain.chars().take(BUBBLE_CHARS).collect();
        out = cut.rsplit_once(' ').map_or(cut.clone(), |(a, _)| a.to_string());
    }
    format!("{} …", out.trim())
}

impl Buddy {
    pub fn new() -> Buddy {
        let bubble = gtk::Label::builder().wrap(true).wrap_mode(gtk::pango::WrapMode::WordChar).xalign(0.0)
            .max_width_chars(38).build();
        bubble.add_css_class("buddy-bubble");
        let face = gtk::DrawingArea::builder().content_width(76).content_height(76).valign(gtk::Align::End).build();
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        row.add_css_class("buddy");
        row.append(&bubble);
        row.append(&face);
        let root = gtk::Revealer::builder().transition_type(gtk::RevealerTransitionType::SlideUp)
            .transition_duration(260).reveal_child(false).child(&row).halign(gtk::Align::End)
            .valign(gtk::Align::End).margin_end(18).margin_bottom(14).build();
        root.set_can_target(false);
        let st = Rc::new(RefCell::new(Look { talk_until: None, speech: None, leave_at: None, hovered: false,
                                             blink_at: Instant::now() + Duration::from_secs(3), t0: Instant::now(),
                                             ticking: false }));
        let b = Buddy { root, bubble, face, st };
        let me = b.clone();
        b.face.set_draw_func(move |_, cr, w, h| me.paint(cr, w as f64, h as f64));
        // a click sends him away; hovering keeps him while you read
        let click = gtk::GestureClick::new();
        let me = b.clone();
        click.connect_released(move |_, _, _, _| me.hide());
        row.add_controller(click);
        let hover = gtk::EventControllerMotion::new();
        let (m1, m2) = (b.clone(), b.clone());
        hover.connect_enter(move |_, _, _| m1.st.borrow_mut().hovered = true);
        hover.connect_leave(move |_| m2.st.borrow_mut().hovered = false);
        row.add_controller(hover);
        row.set_tooltip_text(Some("Chuck — click to send him away"));
        b
    }

    /// Pop up with `text`. `speech`: the voice reading it (his mouth moves
    /// with it); without one he "says" it for as long as reading takes.
    pub fn say(&self, text: &str, speech: Option<(chuck_voice::Voice, u64)>) {
        let g = gist(text);
        if g.is_empty() {
            return;
        }
        self.bubble.set_label(&g);
        let read = Duration::from_millis((g.chars().count() as u64 * 55).clamp(2500, 14_000));
        {
            let mut st = self.st.borrow_mut();
            st.speech = speech.filter(|(_, n)| *n != 0);
            st.talk_until = Some(Instant::now() + if st.speech.is_some() { Duration::from_secs(1) } else {
                read.min(Duration::from_secs(5)) });
            st.leave_at = Some(Instant::now() + read + Duration::from_secs(6));
        }
        self.root.set_can_target(true);
        self.root.set_reveal_child(true);
        self.tick();
    }

    pub fn hide(&self) {
        self.root.set_reveal_child(false);
        self.root.set_can_target(false);
        let mut st = self.st.borrow_mut();
        st.leave_at = None;
        st.talk_until = None;
        if let Some((v, n)) = st.speech.take() {
            if v.speaking(n) {
                v.stop(); // sent away mid-sentence: he stops talking too
            }
        }
    }

    /// Mouth, blink, and leaving: about 12 frames a second while he's out.
    fn tick(&self) {
        if self.st.borrow().ticking {
            return;
        }
        self.st.borrow_mut().ticking = true;
        let me = self.clone();
        glib::timeout_add_local(Duration::from_millis(80), move || {
            let now = Instant::now();
            let leave = {
                let mut st = me.st.borrow_mut();
                // still talking while his voice is
                if let Some((v, n)) = &st.speech {
                    if v.speaking(*n) {
                        st.talk_until = Some(now + Duration::from_millis(200));
                        if let Some(l) = st.leave_at.as_mut() {
                            *l = (*l).max(now + Duration::from_secs(5));
                        }
                    }
                }
                if st.hovered {
                    if let Some(l) = st.leave_at.as_mut() {
                        *l = (*l).max(now + Duration::from_secs(3));
                    }
                }
                if now >= st.blink_at + Duration::from_millis(140) {
                    st.blink_at = now + Duration::from_millis(2500 + (st.t0.elapsed().as_millis() % 3000) as u64);
                }
                st.leave_at.is_some_and(|l| now >= l)
            };
            me.face.queue_draw();
            if leave {
                me.hide();
            }
            if !me.root.reveals_child() {
                me.st.borrow_mut().ticking = false;
                return glib::ControlFlow::Break;
            }
            glib::ControlFlow::Continue
        });
    }

    fn paint(&self, cr: &cairo::Context, w: f64, h: f64) {
        let st = self.st.borrow();
        let now = Instant::now();
        let t = now.duration_since(st.t0).as_secs_f64();
        let talking = st.talk_until.is_some_and(|u| now < u);
        let blink = now >= st.blink_at && now < st.blink_at + Duration::from_millis(140);
        let (cx, cy, r) = (w / 2.0, h / 2.0 + if talking { (t * 9.0).sin() * 1.2 } else { 0.0 }, w.min(h) / 2.0 - 3.0);
        let rgb = |c: u32| (((c >> 16) & 255) as f64 / 255.0, ((c >> 8) & 255) as f64 / 255.0, (c & 255) as f64 / 255.0);
        let fill = |c: u32| {
            let (r, g, b) = rgb(c);
            cr.set_source_rgb(r, g, b);
        };
        // a purple ring, the app's own
        cr.arc(cx, cy, r + 2.0, 0.0, 2.0 * PI);
        fill(0x7C3AED);
        let _ = cr.fill();
        cr.arc(cx, cy, r, 0.0, 2.0 * PI);
        fill(0x17151F);
        let _ = cr.fill();
        // the face
        let fr = r * 0.62;
        let fy = cy + r * 0.06;
        cr.save().ok();
        cr.arc(cx, fy, fr, 0.0, 2.0 * PI);
        fill(0xE6B08A);
        let _ = cr.fill();
        // hair: a swept brown cap
        cr.arc(cx, fy - fr * 0.42, fr * 0.92, PI * 1.08, PI * 1.92);
        cr.close_path();
        fill(0x5A3A22);
        let _ = cr.fill();
        // the beard: from cheek to cheek, round under the chin
        cr.move_to(cx - fr * 0.98, fy + fr * 0.05);
        cr.curve_to(cx - fr * 0.95, fy + fr * 1.25, cx + fr * 0.95, fy + fr * 1.25, cx + fr * 0.98, fy + fr * 0.05);
        cr.curve_to(cx + fr * 0.6, fy + fr * 0.32, cx - fr * 0.6, fy + fr * 0.32, cx - fr * 0.98, fy + fr * 0.05);
        fill(0x6B4423);
        let _ = cr.fill();
        // the mouth, open with the words
        let open = if talking { (((t * 14.0).sin() + 1.0) / 2.0) * fr * 0.22 + fr * 0.04 } else { fr * 0.035 };
        cr.save().ok();
        cr.translate(cx, fy + fr * 0.52);
        cr.scale(fr * 0.26, open.max(0.5));
        cr.arc(0.0, 0.0, 1.0, 0.0, 2.0 * PI);
        cr.restore().ok();
        fill(0x2A1408);
        let _ = cr.fill();
        // eyes (a blink is a line) and stern brows
        for side in [-1.0, 1.0] {
            let ex = cx + side * fr * 0.36;
            let ey = fy - fr * 0.02;
            if blink {
                cr.set_line_width(1.6);
                fill(0x2A1408);
                cr.move_to(ex - fr * 0.13, ey);
                cr.line_to(ex + fr * 0.13, ey);
                let _ = cr.stroke();
            } else {
                cr.arc(ex, ey, fr * 0.12, 0.0, 2.0 * PI);
                fill(0xFFFFFF);
                let _ = cr.fill();
                cr.arc(ex + fr * 0.02, ey + fr * 0.02, fr * 0.065, 0.0, 2.0 * PI);
                fill(0x1A1A1A);
                let _ = cr.fill();
            }
            cr.set_line_width(2.2);
            fill(0x4A2E18);
            cr.move_to(ex - side * fr * 0.2, ey - fr * 0.3);
            cr.line_to(ex + side * fr * 0.16, ey - fr * 0.2);
            let _ = cr.stroke();
        }
        cr.restore().ok();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bubble_holds_the_gist_in_plain_words() {
        assert_eq!(gist("**Fixed.** `parse('')` now returns `[]`."), "Fixed. parse('') now returns [].");
        assert_eq!(gist("Done:\n- `a.py`: guard\n- tests added"), "Done: a.py: guard. tests added.");
        let long = "First sentence here. ".repeat(30);
        let g = gist(&long);
        assert!(g.chars().count() <= BUBBLE_CHARS + 2 && g.ends_with('…') && g.starts_with("First sentence here."), "{g}");
        assert!(gist("   ").is_empty());
    }
}
