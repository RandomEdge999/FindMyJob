#set page(
  paper: "us-letter",
  margin: (left: 0.6in, right: 0.6in, top: 0.55in, bottom: 0.55in),
)
#set text(size: 10pt, font: "New Computer Modern")
#set par(justify: true, leading: 0.86em)

#let ctx = json(sys.inputs.context)
#let job = ctx.job
#let profile = ctx.profile
#let contact = if profile.contact.len() > 0 { profile.contact.at(0) } else { (: ) }
#let cover = ctx.at("cover_letter", default: (: ))
#let paragraphs = cover.at("paragraphs", default: ())
#let company = job.at("company_name", default: job.at("company", default: "Company"))
#let salutation = cover.at("salutation", default: "Dear " + company + " Hiring Team,")
#let closing = cover.at("closing", default: "Sincerely,")
#let signature = cover.at("signature_name", default: contact.at("name", default: ""))
#let education = if profile.education.len() > 0 { profile.education.at(0) } else { (: ) }
#let portfolio_url = contact.at("portfolio", default: none)
#let website_url = contact.at("website", default: none)
#let show_website = website_url != none and website_url != portfolio_url

#salutation

#v(14pt)
#for item in paragraphs [
  #item
  #v(10pt)
]

#v(4pt)
#closing

#v(14pt)
#signature

#if education != (: ) [
  #text(size: 9pt)[
    #education.at("school", default: "")
    #if education.at("degree", default: none) != none [, #education.degree]
    #if education.at("date_label", default: none) != none [, #education.date_label]
  ]
]

#text(size: 9pt)[
  #contact.at("phone", default: "")
  #if contact.at("email", default: none) != none [ | #link("mailto:" + contact.email)[#contact.email]]
]
#text(size: 9pt)[
  #if contact.at("linkedin", default: none) != none [#link(contact.linkedin)[#contact.linkedin]]
  #if contact.at("github", default: none) != none [ | #link(contact.github)[#contact.github]]
]
#if portfolio_url != none [
  #text(size: 9pt)[#link(portfolio_url)[#portfolio_url]]
] else if show_website [
  #text(size: 9pt)[#link(website_url)[#website_url]]
]
