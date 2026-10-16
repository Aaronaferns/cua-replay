# Review card: Lookup customer outstanding

`dolibarr.lookup_customer_outstanding@1.0.0` | effect **read** | app `dolibarr`

- lock hash: `6b9488eec2466f2c5bcef60c45968d4e75304af7f41963fc202a090100d51341`
- discovered by: claude-opus-5-5 (compiler `cua.compiler/v0`, trace `discovery_lookup_customer_outstanding`, sha256 `1d65bc52793d5c494b4f68e74be666020764a8474cbbd331f617287393e84dcc`)
- approval binds to the lock hash above: any change to the capability or the app profile makes it stale

Find the current outstanding bill amount of the customer with customer code <customer_code>.

## Contract

### Inputs

| name | type | pattern | sensitivity | description |
|---|---|---|---|---|
| `customer_code` | string | `CU\d{4}-\d{4,5}` | pii | the customer code |

### Outputs

| name | type | pattern | sensitivity | description |
|---|---|---|---|---|
| `outstanding_amount` | decimal |  | internal | the current outstanding bill amount |

### Business outcomes

- `customer_not_found`: Customers list whose filter matched nothing: the table body says 'No record found'.
- `access_denied`: Dolibarr's 'Access denied' page, shown at the URL that was refused.

## Steps

1. `s01_navigate`: Open the third-party list filtered by the customer code.
   - starts in state `home`; navigate to `/societe/list.php?search_customer_code=${customer_code}`; effect **read**, navigates; timeout 10000 ms
2. `s02_click`: Open the customer card for the matching third party, which shows the current outstanding bill.
   - starts in state `customer_list`; click `link_c`; effect **read**, navigates; timeout 10000 ms
3. `s03_assert`: The screen shows the requested <customer_code>
   - starts in state `customer_tab`; assert that target `cell_customer_code` equals input `customer_code` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
4. `s04_extract`: Use the element with the stable "Current outstanding bill" label, whose text includes the amount.
   - starts in state `customer_tab`; read `generic_current_outstanding_bill` into output `outstanding_amount`; effect **read**; timeout 10000 ms

Commit steps: none

## Targets

### `link_c`

role link "C"

- primary: role link "C"  (role and exact name)
- fallback 1: link in column "Nature of Third party" of the row keyed "${customer_code}"  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 2: link in column "Nature of Third party" of the only row  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 3: link "C" within columnheader "Nature of Third party"  (scoped by the container it sits in); used only if it agrees with the fingerprint
- fallback 4: link within columnheader "Nature of Third party"  (scoped by the container it sits in); used only if it agrees with the fingerprint
- fallback 5: role link "c"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fallback 6: css a.customer-back  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=link, tag=a, column_header=Nature of Third party

### `cell_customer_code`

value in the row labelled "Customer Code"

- primary: value in the row labelled "Customer Code"  (the row's label cell leads to it)
- fingerprint: role=cell, tag=td, row_label=Customer Code

### `generic_current_outstanding_bill`

role generic "Current outstanding bill"

- primary: role generic "Current outstanding bill"  (role and exact name)
- fingerprint: role=generic, tag=div

## Success checkpoint

- ends in state `customer_tab` (or a specialization of it)
- outputs: `outstanding_amount`
- check: target `cell_customer_code` is found exactly once
- check: target `generic_current_outstanding_bill` is found exactly once

## States used

- `home`: Home dashboard. (origin observed; layer page)
  - recognised when all of: the URL matches `/index.php*`; some text equals "Global view"
- `customer_list`: Customers list (third parties filtered to customers). (origin observed; layer page)
  - recognised when all of: the URL matches `/societe/list.php*`; some text (columnheader) equals "Third-party name"
- `customer_tab`: Third-party 'Customer' tab: customer code plus the outstanding-bill summary. (origin observed; layer page)
  - recognised when all of: the URL matches `/comm/card.php*`; some text (cell) equals "Customer Code"
- `login`: Login form. Shown at whatever URL was requested once the session is gone, so it is recognised by its fields, never by URL. (origin observed; layer page; default meaning: failure `SESSION_EXPIRED`)
  - recognised when all of: some text (textbox) equals "Login"; some text (textbox) equals "Password"; some text (button) equals "Login"
