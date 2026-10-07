// Category-aware suggestions for "Other details with AI" on New Run. Pulled out of app.js (same pattern as
// sheetops.js) so the matching logic - which has real, easy-to-get-wrong edge cases (see tests/fieldSuggestions.test.js) -
// is covered by a real test instead of only a visual check in the browser.
(function (root) {
  // Plain Emails/Phone/Address/Website are generic - they fit any business, but miss what actually matters for a
  // temple, a hospital, a hotel... These are suggested AI details, matched against the typed searches and any
  // ticked directory source's category, not forced on anyone: they still cost an AI key to actually run, same as
  // any other custom detail. `placesCategory`, where set, is the exact category Places mode offers for the same
  // kind of place - used to point someone at Places mode when it would genuinely do better than typing searches
  // one city at a time.
  const FIELD_SUGGESTIONS = [
    { label: "Temples", keywords: ["temple", "mandir", "devasthanam", "swamy temple"], placesCategory: "Hindu temples",
      fields: ["Deity", "Darshan timings", "Major festivals", "Managed by (trust/devasthanam)"] },
    { label: "Churches", keywords: ["church"], placesCategory: "Churches",
      fields: ["Denomination", "Service timings", "Managed by (diocese/parish)"] },
    { label: "Mosques", keywords: ["mosque", "masjid"], placesCategory: "Mosques",
      fields: ["Managed by (committee/trust)", "Prayer / Namaz timings"] },
    { label: "Gurudwaras", keywords: ["gurudwara", "gurdwara"], placesCategory: "Gurudwaras",
      fields: ["Managed by (committee)", "Langar timings"] },
    { label: "Hospitals", keywords: ["hospital", "nursing home", "medical cent"], placesCategory: "Hospitals",
      fields: ["Specialities", "Emergency services available", "Visiting hours", "Number of beds"] },
    { label: "Clinics & doctors", keywords: ["clinic", "dispensary", " doctor"], placesCategory: "Clinics & doctors",
      fields: ["Specialities", "Consultation timings", "Appointment required"] },
    { label: "Dentists", keywords: ["dentist", "dental clinic", "dental hospital"],
      fields: ["Specialities", "Appointment required"] },
    { label: "Diagnostic labs", keywords: ["diagnostic cent", "pathology lab", "scan cent", "mri cent"],
      fields: ["Tests offered", "Home sample collection", "Report turnaround time"] },
    { label: "Pharmacies", keywords: ["pharmacy", "medical store", "chemist"], placesCategory: "Pharmacies",
      fields: ["Home delivery available", "24-hour service"] },
    { label: "Veterinary & pet care", keywords: ["veterinary", " vet clinic", "pet clinic", "pet shop", "pet store"], placesCategory: "Veterinary clinics",
      fields: ["Animals treated", "Emergency service", "Boarding available"] },
    { label: "Schools", keywords: ["school"], placesCategory: "Schools",
      fields: ["Board (CBSE/ICSE/State)", "Grades offered", "Admission process", "Medium of instruction"] },
    { label: "Colleges", keywords: ["college", "university", "engineering college", "polytechnic"], placesCategory: "Colleges & universities",
      fields: ["Courses offered", "Affiliated university", "Established year", "Accreditation"] },
    { label: "Coaching & tuition centres", keywords: ["coaching cent", "tuition cent", "tutorial", "exam prep"],
      fields: ["Courses/exams covered", "Batch timings", "Online classes available"] },
    { label: "Daycare & playschools", keywords: ["daycare", "play school", "preschool", "nursery school"],
      fields: ["Age groups", "Timings", "Meals provided"] },
    { label: "Driving schools", keywords: ["driving school", "motor training"], fields: ["Vehicle types taught", "Licence types", "Course duration"] },
    { label: "Restaurants", keywords: ["restaurant", "dhaba", "eatery"], placesCategory: "Restaurants",
      fields: ["Cuisine", "Price range", "Opening hours", "Home delivery available"] },
    { label: "Cafes & bakeries", keywords: ["cafe", "bakery", "coffee shop"], placesCategory: "Cafes",
      fields: ["Specialities", "Price range", "Opening hours"] },
    { label: "Hotels", keywords: ["hotel", "resort", "lodge", "guest house", "homestay"], placesCategory: "Hotels",
      fields: ["Star rating", "Room types", "Check-in / check-out time", "Amenities"] },
    { label: "Real estate", keywords: ["real estate", "property deal", "builders", "apartments for sale", "plots for sale"],
      fields: ["Property types", "Price range", "RERA number"] },
    { label: "Interior designers", keywords: ["interior design", "interior decorator"],
      fields: ["Services offered", "Project types", "Starting price"] },
    { label: "Packers & movers", keywords: ["packers and movers", "relocation service", "house shifting"],
      fields: ["Service area", "Vehicle types", "Insurance offered"] },
    { label: "Gyms & fitness", keywords: ["gym", "fitness cent", "yoga studio", "crossfit"],
      fields: ["Membership plans", "Trainers available", "Timings"] },
    { label: "Salons & spas", keywords: ["salon", " spa", "parlour", "parlor", "beauty cent"],
      fields: ["Services offered", "Price range", "Timings"] },
    { label: "Dry cleaners & laundry", keywords: ["dry clean", "laundry service"], placesCategory: "Dry cleaners & laundry",
      fields: ["Services offered", "Pickup/delivery available", "Turnaround time"] },
    { label: "IT companies", keywords: ["it compan", "software compan", "it services", "it firm"], placesCategory: "IT companies",
      fields: ["Services offered", "Technologies used", "Founded year"] },
    { label: "Offices & companies", keywords: ["companies in", "firms in", "offices in"], placesCategory: "Offices / companies",
      fields: ["Industry", "Founded year", "Number of employees"] },
    { label: "Factories & manufacturing", keywords: ["factory", "manufactur", "industrial unit"], placesCategory: "Factories / industrial",
      fields: ["Products made", "Export/import", "Certifications (ISO etc.)"] },
    { label: "Lawyers & legal", keywords: ["lawyer", "advocate", "legal services", "law firm"],
      fields: ["Practice areas", "Courts practised in", "Years of experience"] },
    { label: "CA & tax consultants", keywords: ["chartered accountant", "tax consultant", "ca firm", "gst consultant"],
      fields: ["Services offered", "Years of experience"] },
    { label: "Insurance agents", keywords: ["insurance agent", "insurance advisor", "insurance broker"],
      fields: ["Insurance types offered", "Companies represented"] },
    { label: "Travel agents", keywords: ["travel agent", "tour operator", "travel agency"],
      fields: ["Destinations covered", "Services offered (visa/tickets/packages)"] },
    { label: "Event planners & photographers", keywords: ["event planner", "wedding planner", "photographer", "banquet hall", "marriage hall"],
      fields: ["Services offered", "Capacity (for venues)", "Starting price"] },
    { label: "Jewellery shops", keywords: ["jewellery", "jewelry shop", "gold shop"], fields: ["Types sold (gold/silver/diamond)", "Making charges", "Hallmark certified"] },
    { label: "Car & bike dealers", keywords: ["car dealer", "car showroom", "bike showroom", "automobile dealer"],
      fields: ["Brands sold", "New/used vehicles", "Service offered"] },
    { label: "Banks & ATMs", keywords: ["bank branch", " atm "], placesCategory: "Banks",
      fields: ["Services offered", "Branch timings"] },
    { label: "Co-working spaces", keywords: ["co-working", "coworking space", "shared office"], placesCategory: "Co-working spaces",
      fields: ["Seating capacity", "Pricing plans", "Amenities"] },
  ];

  // The longest matching keyword wins, not whichever entry happens to be listed first - "dental clinic" (a
  // Dentists keyword) must beat the generic "clinic" (a Clinics & doctors keyword) even though Clinics is
  // listed earlier, and the same for "driving school" beating the generic "school", and so on. Without this,
  // which category "won" would silently depend on array order instead of which keyword actually fits best.
  function matchByText(text) {
    // Padded with a boundary space on both ends so a keyword that needs to be a whole word (" atm ") still
    // matches when it's the very first or very last word of the text - a plain word boundary has nothing to
    // match against there otherwise.
    const padded = ` ${(text || "").toLowerCase()} `;
    let best = null, bestLen = 0;
    for (const fs of FIELD_SUGGESTIONS) {
      const hit = fs.keywords.find((k) => padded.includes(k));
      if (hit && hit.length > bestLen) { best = fs; bestLen = hit.length; }
    }
    return best;
  }

  // A source with no category set (empty string) must never match every entry just because "".includes(x) is
  // trivially false and x.includes("") is trivially true - skip anything that isn't a real category first.
  function matchByCategory(categories) {
    const cats = (categories || []).map((c) => (c || "").toLowerCase()).filter(Boolean);
    if (!cats.length) return null;
    return FIELD_SUGGESTIONS.find((fs) => cats.some((c) => c.includes(fs.label.toLowerCase()) || fs.label.toLowerCase().includes(c))) || null;
  }

  function matchByPlacesCategory(placesCategory) {
    if (!placesCategory) return null;
    return FIELD_SUGGESTIONS.find((fs) => fs.placesCategory === placesCategory) || null;
  }

  const api = { FIELD_SUGGESTIONS, matchByText, matchByCategory, matchByPlacesCategory };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FieldSuggestions = api;
})(typeof window !== "undefined" ? window : globalThis);
