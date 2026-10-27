//! chuck-ui — the GTK4 window: talk to the Lead, watch the whole fleet work
//! in an organised side panel, see the Done gate fill in.

mod chat;
pub mod doctor;
pub mod update;
mod md;
mod panel;
mod style;
mod window;

pub use window::Options;

use adw::prelude::*;

/// Open the window; returns when it's closed — and nothing the fleet started
/// is left running.
pub fn run(opts: Options) -> i32 {
    let app = adw::Application::builder().application_id("org.thepriest.chucknorris")
        .flags(gtk::gio::ApplicationFlags::NON_UNIQUE).build();
    app.connect_activate(move |app| window::build(app, opts.clone()));
    let code = app.run_with_args::<&str>(&[]).value();
    chuck_tools::kill_all();
    code
}
