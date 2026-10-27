//! `cargo run -p chuck-web --example web -- search <query>` or `-- fetch <url>`:
//! the agents' web tools by hand, to check a network or a proxy.

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let web = chuck_web::Web::new(chuck_web::Config::default());
    match (args.first().map(String::as_str), args[1..].join(" ")) {
        (Some("search"), q) => match web.search(&q, 8) {
            Ok(hits) => hits.iter().for_each(|h| println!("{}\n  {}\n  {}\n", h.title, h.url, h.snippet)),
            Err(e) => eprintln!("search failed: {e}"),
        },
        (Some("fetch"), u) => match web.fetch(&u) {
            Ok(p) => println!("{}{}\n\n{}", p.url, p.title.map(|t| format!(" — {t}")).unwrap_or_default(), chuck_web::window(&p.text, 1, 3000).0),
            Err(why) => eprintln!("fetch failed: {why}"),
        },
        _ => eprintln!("usage: web search <query> | web fetch <url>"),
    }
}
