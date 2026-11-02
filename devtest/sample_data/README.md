# Sample Data for Kitebase Library Demo

This directory contains sample data to populate the Kitebase library database with realistic books, authors, and publishers.

## 🚀 Usage

### Prerequisites

1. Update the model with new fields:
   ```bash
   cd /home/claudio/sviluppo/python/webapp/kitebase/devtest
   python devtest.py  # Regenerates model.py with Publisher and new Book fields
   ```

2. Make sure the database is initialized (run server_flask.py or devtest.py once)

### Import Data

```bash
# From the devtest directory
python sample_data/import_sample_data.py
```


## Order sheets (acquisitions)

Spreadsheets for the import of purchase orders, the bench of the document
form (`plugins/libapp/acquisitions`). The first row names the columns; the
books are those of `books.csv`, found by ISBN, with or without dashes.

| File | What it shows |
|---|---|
| `purchase_orders.xlsx` | Our layout: `order` groups the rows into documents (two orders), `publisher` by name, `title` only for whoever reads the sheet |
| `purchase_orders_errors.xlsx` | The same layout with one mistake of each kind: an ISBN nobody has, a quantity in words, an ISBN two books share, a misspelt publisher, a discount over 100 |
| `publisher_order.xlsx` | A publisher's own order form: its column names (`EAN`, `Qty`, `Disc.`), the discount as text, a column we do not use, no publisher column. Our layout refuses it at the header; a derived plugin reads it |
