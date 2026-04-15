#set page(
  paper: "us-letter",
  margin: (left: 0.5in, right: 0.5in, top: 0.34in, bottom: 0.38in),
)
#set text(size: 10pt, font: "New Computer Modern")
#set par(justify: true)

#let ctx = json(sys.inputs.context)
#let job = ctx.job
#let profile = ctx.profile
#let contact = if profile.contact.len() > 0 { profile.contact.at(0) } else { (: ) }
#let draft = ctx.at("resume_draft", default: (: ))
#let portfolio_url = contact.at("portfolio", default: none)
#let website_url = contact.at("website", default: none)
#let show_website = website_url != none and website_url != portfolio_url

// --- Contact Header ---
#align(center)[
  #text(16pt, weight: "bold")[#contact.name] \
  #text(size: 9pt)[
    #contact.at("phone", default: "")
    #if contact.at("email", default: none) != none [ | #link("mailto:" + contact.email)[#contact.email]]
    #if contact.at("linkedin", default: none) != none [ | #link(contact.linkedin)[#contact.linkedin]]
    #if contact.at("github", default: none) != none [ | #link(contact.github)[#contact.github]]
    #if portfolio_url != none [ | #link(portfolio_url)[#portfolio_url]]
    #if show_website [ | #link(website_url)[#website_url]]
  ]
]

// --- Section heading helper ---
#let section-heading(title) = {
  v(2pt)
  text(size: 11pt, weight: "regular", style: "normal")[#smallcaps(title)]
  v(-6pt)
  line(length: 100%, stroke: 0.5pt)
  v(2pt)
}

// --- Research Summary ---
#let summary-lines = draft.at("summary_lines", default: ())
#if summary-lines.len() > 0 {
  section-heading("Research Summary")
  text(size: 9pt)[#summary-lines.join(" ")]
}

// --- Education ---
#if profile.education.len() > 0 {
  section-heading("Education")
  for item in profile.education {
    grid(
      columns: (1fr, auto),
      text(weight: "bold")[#item.at("school", default: "")],
      text(size: 9pt)[#item.at("dates", default: item.at("date_label", default: item.at("graduation", default: "")))],
    )
    if item.at("degree", default: none) != none {
      text(size: 9pt)[#item.degree]
    }
    if item.at("summary", default: none) != none and item.summary != item.at("degree", default: "") {
      text(size: 9pt)[#item.summary]
    }
    for bullet in item.at("coursework", default: ()) {
      text(size: 9pt)[- #bullet]
    }
    for bullet in item.at("highlights", default: ()) {
      text(size: 9pt)[- #bullet]
    }
    v(4pt)
  }
}

// --- Work / Experience ---
#if profile.work.len() > 0 {
  section-heading("Experience")
  for item in profile.work {
    grid(
      columns: (1fr, auto),
      text(weight: "bold")[#item.at("title", default: "")],
      text(size: 9pt)[#item.at("dates", default: "")],
    )
    grid(
      columns: (1fr, auto),
      text(size: 9pt, style: "italic")[#item.at("company", default: "")],
      text(size: 9pt)[#item.at("location", default: "")],
    )
    if item.at("summary", default: none) != none {
      let bullets = item.summary.split("\n").filter(b => b.trim() != "")
      for bullet in bullets.slice(0, calc.min(5, bullets.len())) {
        text(size: 9pt)[- #bullet.trim()]
      }
    }
    v(4pt)
  }
}

// --- Projects ---
#if profile.projects.len() > 0 {
  section-heading("Selected Projects")
  for item in profile.projects {
    grid(
      columns: (1fr, auto),
      text(weight: "bold")[#item.at("name", default: item.at("title", default: "Project"))],
      text(size: 9pt)[#item.at("dates", default: "")],
    )
    if item.at("tech", default: none) != none {
      text(size: 9pt, style: "italic")[#item.tech]
    }
    if item.at("summary", default: none) != none {
      let bullets = item.summary.split("\n").filter(b => b.trim() != "")
      for bullet in bullets.slice(0, calc.min(3, bullets.len())) {
        text(size: 9pt)[- #bullet.trim()]
      }
    }
    v(4pt)
  }
}

// --- Skills ---
#if profile.skills.len() > 0 {
  section-heading("Skills")
  // Group by category if available
  let categorized = (:)
  for item in profile.skills {
    let cat = item.at("category", default: "General")
    if cat not in categorized { categorized.insert(cat, ()) }
    categorized.at(cat).push(item.at("name", default: ""))
  }
  if categorized.len() > 1 {
    for (cat, names) in categorized {
      text(size: 8.5pt)[*#cat:* #names.filter(n => n != "").join(", ")]
      v(2pt)
    }
  } else {
    let all-names = profile.skills.map(s => s.at("name", default: "")).filter(n => n != "")
    text(size: 8.5pt)[#all-names.join(", ")]
  }
}
