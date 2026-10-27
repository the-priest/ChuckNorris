//! The welcome screen's background: the fabric Chuck is woven from. A port
//! of the Python app's GlyphField.
//!
//! A hidden net of tiny code glyphs. At rest nothing is drawn (no idle
//! cost). Move the pointer and a soft lens of the fabric surfaces under it,
//! bulging gently and settling when you stop. Click and the fabric is
//! struck: a lobed wavefront (its own shape per strike, drifting as it
//! travels) rolls out, pushing glyphs ahead of it out and pulling those
//! behind back with a sideways weave; neighbours on the crest are joined by
//! faint threads; a cool white leading edge, a violet crest and an indigo
//! wake where characters keep decoding as it fades. Frames visit only the
//! glyphs near each front, and the wake thins out when frames run long.
//! Honours the desktop's reduce-animation setting.

use gtk::prelude::*;
use gtk::{cairo, glib};
use std::cell::RefCell;
use std::collections::HashMap;
use std::f64::consts::PI;
use std::rc::Rc;
use std::time::Instant;

const CHARS: &str = "{}[]()<>=;:+-*/#&|~$%01λ0123456789abcdef";
const CELL_W: f64 = 7.0;
const CELL_H: f64 = 9.0;
const FONT: f64 = 6.0;
const SPEED: f64 = 300.0;
const LIFE: f64 = 2.6;
const LEAD: f64 = 7.0;
const WAKE: f64 = 58.0;
const LOBE: f64 = 0.24;
const PUSH: f64 = 6.0;
const WEAVE: f64 = 3.0;
/// Each strike sends a main front and a slower, fainter echo:
/// (speed, strength, shape seed offset, wake length).
const FRONTS: [(f64, f64, u64, f64); 2] = [(1.0, 1.0, 0, 58.0), (0.6, 0.5, 17, 16.0)];
const BLOOM_R: f64 = 46.0;
const BLOOM_T: f64 = 0.45;
const LEVELS: usize = 6;
const MAX_WAVES: usize = 4;
const ANGLES: usize = 128;
const LENS_R: f64 = 120.0;
const LENS_FADE: f64 = 0.9;
const STEP: i32 = 320;
/// (lead, crest, wake) colours: cool white, violet, indigo.
const BANDS: [(f64, f64, f64); 3] = [(0.86, 0.90, 1.00), (0.70, 0.55, 1.00), (0.42, 0.36, 0.86)];

/// A strike's front: where, when, its seed, and which of FRONTS.
type Front = (f64, f64, f64, u64, (f64, f64, u64, f64));

#[derive(Clone, Copy)]
struct Glyph {
    x: f64,
    y: f64,
    ci: usize,
    bright: f64,
    seed: u64,
}

/// What a glyph is doing this frame: intensity, push, age, band.
#[derive(Clone, Copy)]
struct Lit {
    k: f64,
    dx: f64,
    dy: f64,
    age: f64,
    band: usize,
}

#[derive(Default)]
struct Field {
    ripples: Vec<(f64, f64, f64, u64)>,
    pointer: Option<(f64, f64, f64)>,
    lens_since: f64,
    lens_shape: Option<(f64, Vec<f64>)>,
    strikes: u64,
    size: (i32, i32),
    rows: Vec<Vec<Option<Glyph>>>,
    gids: Option<Vec<u64>>,
    lead: Vec<f64>,
    wake: Vec<f64>,
    ticking: bool,
    /// 0-3: how much of the wake to skip when frames run slow.
    thin: u64,
    frame_ms: f64,
}

fn now() -> f64 {
    thread_local!(static T0: Instant = Instant::now());
    T0.with(|t| t.elapsed().as_secs_f64())
}

fn animations_on() -> bool {
    gtk::Settings::default().is_none_or(|s| s.is_gtk_enable_animations())
}

/// A small deterministic generator (the net looks the same every time).
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
    fn below(&mut self, n: usize) -> usize {
        ((self.next() * n as f64) as usize).min(n - 1)
    }
}

impl Field {
    fn new() -> Field {
        Field {
            lead: (0..=(LEAD * 3.0) as usize).map(|o| (-(o as f64 / LEAD).powi(2)).exp()).collect(),
            wake: (0..=WAKE as usize).map(|o| (-(o as f64) / (WAKE * 0.38)).exp()).collect(),
            ..Field::default()
        }
    }

    fn ensure(&mut self, w: i32, h: i32) {
        let ww = (w / STEP + 1) * STEP;
        let hh = (h / STEP + 1) * STEP;
        if (ww, hh) == self.size && !self.rows.is_empty() {
            return;
        }
        let mut rnd = Rng(0x0C4C);
        let (cx, cy) = (w as f64 / 2.0, h as f64 * 0.42);
        let maxd = cx.max(w as f64 - cx).hypot(cy.max(h as f64 - cy)).max(1.0);
        let n = CHARS.chars().count();
        let mut rows = Vec::new();
        for r in 0..=(hh as f64 / CELL_H) as usize {
            let mut row = Vec::new();
            for c in 0..=(ww as f64 / CELL_W) as usize {
                if rnd.next() < 0.1 {
                    row.push(None); // a few gaps keep it organic
                    continue;
                }
                let x = c as f64 * CELL_W + 1.0 + (r % 2) as f64 * (CELL_W / 2.0);
                let y = r as f64 * CELL_H + 7.0;
                let d = (x - cx).hypot(y - cy) / maxd;
                // dimmer behind the content in the middle, brighter toward the edges
                let bright = (0.55 + rnd.next() * 0.45) * (0.5 + 0.5 * (d * 1.25).min(1.0));
                row.push(Some(Glyph { x, y, ci: rnd.below(n), bright, seed: rnd.below(997) as u64 }));
            }
            rows.push(row);
        }
        self.rows = rows;
        self.size = (ww, hh);
    }

    fn lens_strength(&self, now: f64) -> f64 {
        let Some((_, _, t)) = self.pointer else { return 0.0 };
        let idle = now - t;
        let rise = ((now - self.lens_since) / 0.25).min(1.0);
        rise * (1.0 - (idle - 0.35).max(0.0) / LENS_FADE).max(0.0)
    }

    /// This strike's radius multiplier per angle bin: smooth, lobed, and
    /// drifting with time, so no two strikes spread alike.
    fn front(seed: u64, age: f64) -> Vec<f64> {
        let mut rnd = seed.wrapping_mul(2654435761) & 0xFFFF_FFFF;
        let mut terms = Vec::new();
        for k in [2.0f64, 3.0, 5.0, 7.0] {
            rnd = (rnd.wrapping_mul(1103515245).wrapping_add(12345)) & 0x7FFF_FFFF;
            let ph = rnd as f64 / 0x7FFF_FFFF as f64 * 2.0 * PI;
            rnd = (rnd.wrapping_mul(1103515245).wrapping_add(12345)) & 0x7FFF_FFFF;
            let amp = (0.35 + rnd as f64 / 0x7FFF_FFFF as f64 * 0.65) / k.powf(0.7);
            let drift = if k as i64 % 2 == 1 { 1.0 } else { -1.0 } * 0.5 / k;
            terms.push((k, ph, amp, drift));
        }
        let norm: f64 = terms.iter().map(|t| t.2).sum::<f64>().max(1e-9);
        (0..ANGLES).map(|i| {
            let th = i as f64 / ANGLES as f64 * 2.0 * PI;
            let v: f64 = terms.iter().map(|(k, p, a, s)| a * (k * th + p + s * age * 2.0 * PI).sin()).sum();
            1.0 + LOBE * v / norm
        }).collect()
    }

    /// Every glyph a strike (or the pointer's lens) is on.
    fn lit(&mut self, now: f64) -> HashMap<usize, Lit> {
        let mut hit: HashMap<usize, Lit> = HashMap::new();
        let (cw, ch) = (CELL_W, CELL_H);
        let nlead = self.lead.len() - 1;
        let sparse = 5 + 3 * self.thin;
        let nrows = self.rows.len();
        let a = ANGLES;
        let a_k = a as f64 / (2.0 * PI);
        let fronts: Vec<Front> = self.ripples.iter()
            .flat_map(|&(rx, ry, t0, seed)| FRONTS.iter().map(move |f| (rx, ry, t0, seed, *f))).collect();
        for (rx, ry, t0, seed, (fsp, fam, fso, fwk)) in fronts {
            let age = now - t0;
            // the front decelerates a little as it spreads, like a real wave
            let rad = fsp * SPEED * age * (1.0 - 0.18 * age / LIFE);
            let amp = fam * (1.0 - age / LIFE).max(0.0).powf(1.6);
            if amp <= 0.02 {
                continue;
            }
            let nwake = (fwk as usize).min(self.wake.len() - 1);
            let shape = Field::front(seed + fso, age);
            let lo = shape.iter().cloned().fold(f64::MAX, f64::min);
            let hi = shape.iter().cloned().fold(f64::MIN, f64::max);
            // the shape's local extremes around each angle, so a row only scans where the front is near it
            let win = 9usize;
            let ext: Vec<f64> = shape[a - win..].iter().chain(shape.iter()).chain(shape[..win].iter()).cloned().collect();
            let lo_w: Vec<f64> = (0..a).map(|b| ext[b..b + 2 * win + 1].iter().cloned().fold(f64::MAX, f64::min)).collect();
            let hi_w: Vec<f64> = (0..a).map(|b| ext[b..b + 2 * win + 1].iter().cloned().fold(f64::MIN, f64::max)).collect();
            let tick = (age * 18.0) as u64;
            let r1 = (rad * lo - nwake as f64).max(0.0);
            let r2 = rad * hi + nlead as f64;
            let (r1s, r2s) = (r1 * r1, r2 * r2);
            let row_lo = (((ry - r2 - 7.0) / ch).floor().max(0.0)) as usize;
            let row_hi = ((((ry + r2 - 7.0) / ch).floor() + 1.0).max(0.0) as usize).min(nrows.saturating_sub(1));
            let cap2 = (rad * lo * 0.8).powi(2);
            for r in row_lo..=row_hi {
                let y = r as f64 * ch + 7.0;
                let dy = y - ry;
                let dy2 = dy * dy;
                if dy2 > r2s {
                    continue;
                }
                let shift = 1.0 + (r % 2) as f64 * (cw / 2.0);
                let mut deep = -1.0f64;
                let spans: Vec<(f64, f64)> = if dy2 < cap2 {
                    // the row crosses the front twice: bound each crossing locally
                    let cx = (rad * rad - dy2).max(0.0).sqrt();
                    let mut sp = Vec::new();
                    let mut deeps = Vec::new();
                    for side in [-1.0f64, 1.0] {
                        let b = (((dy.atan2(side * cx) + PI) * a_k) as usize) % a;
                        let ri = (rad * lo_w[b] - nwake as f64).max(0.0);
                        let ro = rad * hi_w[b] + nlead as f64;
                        let xi = if ri * ri > dy2 { (ri * ri - dy2).sqrt() } else { 0.0 };
                        let xo = (ro * ro - dy2).max(0.0).sqrt();
                        let rd = rad * lo_w[b] - 12.0;
                        deeps.push(if rd > 0.0 && rd * rd > dy2 { (rd * rd - dy2).sqrt() } else { -1.0 });
                        sp.push(if side < 0.0 { (rx - xo, rx - xi) } else { (rx + xi, rx + xo) });
                    }
                    deep = deeps.iter().cloned().fold(f64::MAX, f64::min); // in the wake on BOTH sides, or nowhere
                    sp
                } else {
                    let outer = (r2s - dy2).sqrt();
                    let inner = if dy2 < r1s { (r1s - dy2).sqrt() } else { 0.0 };
                    // a row through the ring's cap is one span, else one each side
                    if inner == 0.0 { vec![(rx - outer, rx + outer)] } else { vec![(rx - outer, rx - inner), (rx + inner, rx + outer)] }
                };
                let row = &self.rows[r];
                for (xa, xb) in spans {
                    let c0 = ((xa - shift) / cw).floor().max(0.0) as usize;
                    let c1 = ((((xb - shift) / cw).floor() + 1.0).max(0.0) as usize).min(row.len().saturating_sub(1));
                    for (c, g) in row.iter().enumerate().take(c1 + 1).skip(c0) {
                        let Some(g) = g else { continue };
                        let dx = g.x - rx;
                        if -deep < dx && dx < deep && (g.seed + tick) % sparse > 1 {
                            continue; // deep wake, and not its turn to decode
                        }
                        let d = (dx * dx + dy2).sqrt();
                        let th = dy.atan2(dx);
                        let o = d - rad * shape[(((th + PI) * a_k) as usize) % a];
                        let (k, band, wave);
                        if o >= 0.0 {
                            if o >= nlead as f64 {
                                continue;
                            }
                            k = amp * self.lead[o as usize];
                            band = 0;
                            wave = 1.0;
                        } else {
                            let o = -o;
                            if o >= nwake as f64 {
                                continue;
                            }
                            band = if o < 12.0 { 1 } else { 2 };
                            // the wake decodes: a shifting few glyphs at a time
                            if band == 2 && (g.seed + tick) % sparse > 1 {
                                continue;
                            }
                            k = amp * self.wake[o as usize];
                            wave = if o < 28.0 { 1.0 - o / 14.0 } else { -1.0 }; // out, then back
                        }
                        if k < 0.04 {
                            continue;
                        }
                        let e = hit.entry(r * 4096 + c).or_insert(Lit { k: 0.0, dx: 0.0, dy: 0.0, age, band });
                        e.k += k;
                        if band < e.band {
                            e.band = band;
                        }
                        if d > 0.5 {
                            let (ux, uy) = (dx / d, dy / d);
                            let push = k * PUSH * wave;
                            let weave = k * WEAVE * (th * 6.0 + age * 7.0 + g.seed as f64).sin();
                            e.dx += ux * push - uy * weave;
                            e.dy += uy * push + ux * weave;
                        }
                    }
                }
            }
        }
        // the bloom where a strike landed
        for &(rx, ry, t0, _) in &self.ripples {
            let age = now - t0;
            if age >= BLOOM_T {
                continue;
            }
            let fade = (1.0 - age / BLOOM_T).powi(2);
            let rr = BLOOM_R * (0.6 + 0.8 * age / BLOOM_T);
            let r_lo = (((ry - rr - 7.0) / ch).floor().max(0.0)) as usize;
            let r_hi = ((((ry + rr - 7.0) / ch).floor() + 1.0).max(0.0) as usize).min(nrows.saturating_sub(1));
            for r in r_lo..=r_hi {
                let row = &self.rows[r];
                let dy = r as f64 * ch + 7.0 - ry;
                if dy.abs() >= rr {
                    continue;
                }
                let span = (rr * rr - dy * dy).sqrt();
                let shift = 1.0 + (r % 2) as f64 * (cw / 2.0);
                let c0 = ((rx - span - shift) / cw).floor().max(0.0) as usize;
                let c1 = ((((rx + span - shift) / cw).floor() + 1.0).max(0.0) as usize).min(row.len().saturating_sub(1));
                for (c, g) in row.iter().enumerate().take(c1 + 1).skip(c0) {
                    let Some(g) = g else { continue };
                    let f = 1.0 - ((g.x - rx).powi(2) + dy * dy).sqrt() / rr;
                    if f <= 0.0 || (g.seed % 100) as f64 / 100.0 > f * 1.6 {
                        continue; // a soft, grainy edge
                    }
                    let e = hit.entry(r * 4096 + c).or_insert(Lit { k: 0.0, dx: 0.0, dy: 0.0, age, band: 0 });
                    e.k += fade * f * 1.2;
                    e.band = 0;
                }
            }
        }
        // the lens under the pointer
        let lens = self.lens_strength(now);
        if let (true, Some((px, py, _))) = (lens > 0.0, self.pointer) {
            if self.lens_shape.as_ref().is_none_or(|(t, _)| now - t > 0.05) {
                self.lens_shape = Some((now, Field::front(4243, now * 0.25))); // a slowly breathing outline
            }
            let lshape = &self.lens_shape.as_ref().unwrap().1;
            let rr = LENS_R * lshape.iter().cloned().fold(f64::MIN, f64::max);
            let r_lo = (((py - rr - 7.0) / ch).floor().max(0.0)) as usize;
            let r_hi = ((((py + rr - 7.0) / ch).floor() + 1.0).max(0.0) as usize).min(nrows.saturating_sub(1));
            let breathe = 0.85 + 0.15 * (now * 2.4).sin();
            for r in r_lo..=r_hi {
                let row = &self.rows[r];
                let dy = r as f64 * ch + 7.0 - py;
                if dy.abs() > rr {
                    continue;
                }
                let span = (rr * rr - dy * dy).sqrt();
                let shift = 1.0 + (r % 2) as f64 * (cw / 2.0);
                let c0 = ((px - span - shift) / cw).floor().max(0.0) as usize;
                let c1 = ((((px + span - shift) / cw).floor() + 1.0).max(0.0) as usize).min(row.len().saturating_sub(1));
                for (c, g) in row.iter().enumerate().take(c1 + 1).skip(c0) {
                    let Some(g) = g else { continue };
                    let dx = g.x - px;
                    let d = (dx * dx + dy * dy).sqrt();
                    let edge = LENS_R * lshape[(((dy.atan2(dx) + PI) * a_k) as usize) % a];
                    if d >= edge {
                        continue;
                    }
                    let f = 1.0 - d / edge;
                    // the edge dissolves glyph by glyph instead of stopping at a line
                    if (g.seed % 97) as f64 / 97.0 > f * 2.2 {
                        continue;
                    }
                    let k = lens * 0.5 * f * breathe;
                    if k < 0.03 {
                        continue;
                    }
                    let e = hit.entry(r * 4096 + c).or_insert(Lit { k: 0.0, dx: 0.0, dy: 0.0, age: now - self.lens_since, band: 2 });
                    e.k += k;
                    if d > 0.5 {
                        // a lens: bulge outward, gently
                        let b = lens * 3.2 * f * (1.0 - f);
                        e.dx += dx / d * b;
                        e.dy += dy / d * b;
                    }
                }
            }
        }
        hit
    }

    fn paint(&mut self, cr: &cairo::Context) {
        let hit = self.lit(now());
        if hit.is_empty() {
            return;
        }
        cr.select_font_face("monospace", cairo::FontSlant::Normal, cairo::FontWeight::Bold);
        cr.set_font_size(FONT);
        // grey antialiasing, no hinting: identical at 6px and far cheaper
        if let Ok(mut fo) = cairo::FontOptions::new() {
            fo.set_antialias(cairo::Antialias::Gray);
            fo.set_hint_style(cairo::HintStyle::None);
            fo.set_hint_metrics(cairo::HintMetrics::Off);
            cr.set_font_options(&fo);
        }
        if self.gids.is_none() {
            let sf = cr.scaled_font();
            self.gids = Some(CHARS.chars().map(|c| sf.text_to_glyphs(0.0, 0.0, &c.to_string()).ok()
                .and_then(|(g, _)| g.first().map(|g| g.index())).unwrap_or(0)).collect());
        }
        let gids = self.gids.as_ref().unwrap();
        let n = gids.len();
        let mut buckets: Vec<Vec<Vec<cairo::Glyph>>> = (0..BANDS.len()).map(|_| (0..LEVELS).map(|_| Vec::new()).collect()).collect();
        let mut pos: HashMap<usize, (f64, f64, f64)> = HashMap::new();
        for (&key, l) in &hit {
            let Some(Some(g)) = self.rows.get(key >> 12).and_then(|r| r.get(key & 4095)) else { continue };
            let inten = (l.k * 1.6).min(1.0);
            let v = inten * (0.35 + 0.65 * g.bright);
            if v < 0.05 {
                continue;
            }
            let lvl = ((v * LEVELS as f64) as usize).min(LEVELS - 1);
            let mut ci = g.ci;
            if inten > 0.4 || l.band == 2 {
                // decoding: characters flip
                ci = ((g.seed as f64 + (l.age * if l.band < 2 { 26.0 } else { 9.0 }).floor()) as usize) % n;
            }
            let (gx, gy) = (g.x + l.dx, g.y + l.dy);
            if v >= 0.42 {
                pos.insert(key, (gx + 1.5, gy - 2.0, v));
            }
            buckets[l.band][lvl].push(cairo::Glyph::new(gids[ci], gx, gy));
        }
        // threads: the woven mesh, only where the fabric flexes hardest
        let mut lines: [Vec<(f64, f64, f64, f64)>; 2] = [Vec::new(), Vec::new()];
        let mut budget = 700 / (1 + self.thin as i64);
        for (&key, &(x1, y1, v1)) in &pos {
            let r = key >> 12;
            for nb in [key + 1, (key + 4096).wrapping_sub(if r % 2 == 1 { 0 } else { 1 })] {
                let Some(&(x2, y2, v2)) = pos.get(&nb) else { continue };
                lines[usize::from(v1.min(v2) > 0.7)].push((x1, y1, x2, y2));
                budget -= 1;
            }
            if budget <= 0 {
                break;
            }
        }
        cr.set_line_width(0.6);
        for (i, segs) in lines.iter().enumerate() {
            if segs.is_empty() {
                continue;
            }
            for &(x1, y1, x2, y2) in segs {
                cr.move_to(x1, y1);
                cr.line_to(x2, y2);
            }
            cr.set_source_rgba(0.62, 0.52, 1.0, 0.10 + 0.12 * i as f64);
            let _ = cr.stroke();
        }
        for (band, levels) in buckets.iter().enumerate() {
            let (br, bg, bb) = BANDS[band];
            for (lvl, glyphs) in levels.iter().enumerate() {
                if glyphs.is_empty() {
                    continue;
                }
                let t = (lvl as f64 + 0.5) / LEVELS as f64;
                // dim glyphs lean neutral; bright ones take the band's colour
                let mix = 0.35 + 0.65 * t;
                cr.set_source_rgba(0.72 + (br - 0.72) * mix, 0.72 + (bg - 0.72) * mix, 0.80 + (bb - 0.80) * mix,
                                   (0.14 + t * 0.85).min(0.95));
                let _ = cr.show_glyphs(glyphs);
            }
        }
    }
}

/// The field: a drawing area, and what makes it move.
#[derive(Clone)]
pub struct GlyphField {
    pub area: gtk::DrawingArea,
    f: Rc<RefCell<Field>>,
}

impl GlyphField {
    pub fn new() -> GlyphField {
        let area = gtk::DrawingArea::builder().hexpand(true).vexpand(true).can_target(false).build();
        let f = Rc::new(RefCell::new(Field::new()));
        let f2 = f.clone();
        area.set_draw_func(move |_, cr, w, h| {
            let Ok(mut fl) = f2.try_borrow_mut() else { return };
            if fl.ripples.is_empty() && fl.pointer.is_none() {
                return; // invisible at rest
            }
            fl.ensure(w, h);
            let t = Instant::now();
            fl.paint(cr);
            // keep the frame inside budget on slower machines: when frames run long, the wake thins out
            let ms = t.elapsed().as_secs_f64() * 1000.0;
            fl.frame_ms = 0.7 * fl.frame_ms + 0.3 * ms;
            if fl.frame_ms > 11.0 && fl.thin < 3 {
                fl.thin += 1;
                fl.frame_ms = 8.0;
            } else if fl.frame_ms < 5.0 && fl.thin > 0 {
                fl.thin -= 1;
            }
        });
        GlyphField { area, f }
    }

    fn run(&self) {
        let mut fl = self.f.borrow_mut();
        if !fl.ticking {
            fl.ticking = true;
            let f = self.f.clone();
            self.area.add_tick_callback(move |w, _| {
                let t = now();
                let mut fl = f.borrow_mut();
                fl.ripples.retain(|r| t - r.2 < LIFE);
                if fl.pointer.is_some() && fl.lens_strength(t) <= 0.0 {
                    fl.pointer = None;
                }
                w.queue_draw();
                if fl.ripples.is_empty() && fl.pointer.is_none() {
                    fl.ticking = false;
                    return glib::ControlFlow::Break;
                }
                glib::ControlFlow::Continue
            });
        }
        drop(fl);
        self.area.queue_draw();
    }

    /// A strike at (x, y).
    pub fn poke(&self, x: f64, y: f64) {
        if !animations_on() {
            return;
        }
        {
            let mut fl = self.f.borrow_mut();
            fl.strikes += 1;
            let seed = (fl.strikes * 7919 + (x as u64) * 31 + y as u64) % 100_003;
            fl.ripples.push((x, y, now(), seed));
            let n = fl.ripples.len();
            if n > MAX_WAVES {
                fl.ripples.drain(..n - MAX_WAVES);
            }
        }
        self.run();
    }

    /// The pointer moved (None: it left).
    pub fn hover(&self, at: Option<(f64, f64)>) {
        if !animations_on() {
            return;
        }
        {
            let t = now();
            let mut fl = self.f.borrow_mut();
            match at {
                None => {
                    if let Some(p) = fl.pointer {
                        fl.pointer = Some((p.0, p.1, t - 0.35)); // fade out
                    }
                }
                Some((x, y)) => {
                    if fl.pointer.is_none_or(|p| t - p.2 > LENS_FADE) {
                        fl.lens_since = t;
                    }
                    fl.pointer = Some((x, y, t));
                }
            }
        }
        self.run();
    }

    /// Strike on clicks and follow the pointer anywhere over `widget`,
    /// without taking either from the buttons on top.
    pub fn listen(&self, widget: &impl IsA<gtk::Widget>) {
        let click = gtk::GestureClick::new();
        click.set_propagation_phase(gtk::PropagationPhase::Capture);
        let me = self.clone();
        click.connect_pressed(move |_, _, x, y| me.poke(x, y));
        widget.add_controller(click);
        let motion = gtk::EventControllerMotion::new();
        motion.set_propagation_phase(gtk::PropagationPhase::Capture);
        let me = self.clone();
        motion.connect_motion(move |_, x, y| me.hover(Some((x, y))));
        let me = self.clone();
        motion.connect_leave(move |_| me.hover(None));
        widget.add_controller(motion);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_strike_lights_a_ring_that_moves_out_and_fades() {
        let mut f = Field::new();
        f.ensure(800, 600);
        f.ripples.push((400.0, 300.0, 0.0, 42));
        let early = f.lit(0.2);
        let later = f.lit(1.0);
        assert!(!early.is_empty() && !later.is_empty());
        let mean = |h: &HashMap<usize, Lit>| {
            h.keys().map(|k| {
                let g = f.rows[k >> 12][k & 4095].unwrap();
                (g.x - 400.0).hypot(g.y - 300.0)
            }).sum::<f64>() / h.len() as f64
        };
        assert!(mean(&later) > mean(&early) * 2.0, "the front travels outward");
        assert!(f.lit(LIFE + 0.1).is_empty(), "and is gone once its life is over");
    }

    #[test]
    fn the_lens_follows_the_pointer_and_settles() {
        let mut f = Field::new();
        f.ensure(800, 600);
        f.pointer = Some((100.0, 100.0, 1.0));
        f.lens_since = 0.5;
        let h = f.lit(1.1);
        assert!(!h.is_empty());
        assert!(h.keys().all(|k| {
            let g = f.rows[k >> 12][k & 4095].unwrap();
            (g.x - 100.0).hypot(g.y - 100.0) < LENS_R * 1.4
        }));
        assert!(f.lit(1.0 + 0.35 + LENS_FADE + 0.1).is_empty(), "still pointer: it settles");
    }

    #[test]
    fn fronts_are_lobed_not_circles() {
        let s = Field::front(7, 0.0);
        let (lo, hi) = (s.iter().cloned().fold(9.0, f64::min), s.iter().cloned().fold(0.0, f64::max));
        assert!(hi - lo > 0.1 && lo > 0.6 && hi < 1.4, "{lo} {hi}");
        assert_ne!(Field::front(7, 0.0), Field::front(8, 0.0));
    }
}
