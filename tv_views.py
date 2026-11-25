def paginate(rows, page, per_page):
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    current = min(pages, max(1, page))
    start = (current - 1) * per_page
    return {"rows": rows[start:start + per_page], "current": current, "pages": pages, "total": len(rows)}
