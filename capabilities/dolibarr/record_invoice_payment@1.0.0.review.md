# Review card: Record invoice payment

`dolibarr.record_invoice_payment@1.0.0` | effect **commit** | app `dolibarr`

- lock hash: `bc3d3a7c49c4e27987254135bc17181a9d8de6435a584bc29974635693c461ac`
- discovered by: aaron (compiler `cua.compiler/v0`, trace `discovery_record_payment`, sha256 `4e4e626b024416ae6b105a333115d4bcefc1624cb87046eebaad05ee58eecd0e`)
- approval binds to the lock hash above: any change to the capability or the app profile makes it stale

Record a payment of <amount> on customer invoice <invoice_ref> using payment method <method> into bank account <bank_account>, confirm it, and read the reference of the payment that was created.

## Contract

### Inputs

| name | type | pattern | sensitivity | description |
|---|---|---|---|---|
| `invoice_ref` | string |  | internal |  |
| `amount` | string |  | internal |  |
| `method` | string |  | internal |  |
| `bank_account` | string |  | internal |  |
| `request_id` | string |  | internal | idempotency key of this commit |

### Outputs

| name | type | pattern | sensitivity | description |
|---|---|---|---|---|
| `payment_ref` | string |  | internal |  |

### Business outcomes

- `access_denied`: Dolibarr's 'Access denied' page, shown at the URL that was refused.
- `invoice_not_found`: Invoice list whose filter matched nothing: the table body says 'No record found'.
- `validation_rejected`: Payment confirmation carrying Dolibarr's warning that the amount is higher than the outstanding amount (the confirmation defaults to No).

## Steps

1. `s01_navigate`: Open the customer invoice list filtered by the invoice reference placeholder.
   - starts in state `home`; navigate to `/compta/facture/list.php?mainmenu=billing&leftmenu=customers_bills&search_ref=${invoice_ref}`; effect **read**, navigates; timeout 10000 ms
   - on state `invoice_not_found`: ends with the business outcome `invoice_not_found`
2. `s02_click`: Open the matching invoice card.
   - starts in state `invoice_list`; click `link_invoice_ref`; effect **read**, navigates; timeout 10000 ms
3. `s03_click`: Open the payment entry form for this invoice.
   - starts in state `invoice_card`; click `link_enter_payment`; effect **read**, navigates; timeout 10000 ms
4. `s04_select`: Choose the requested payment method.
   - starts in state `payment_form`; select `${method}` in `combobox_select_selectpaiementcode`; effect **none**; timeout 10000 ms
5. `s05_select`: Choose the requested bank account to credit.
   - starts in state `payment_form`; select `${bank_account}` in `combobox_select_selectaccountid`; effect **none**; timeout 10000 ms
6. `s06_assert`: The screen shows the requested <bank_account>
   - starts in state `payment_form`; assert that target `combobox_select_selectaccountid` equals input `bank_account` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
7. `s07_click`: Set the payment date to today since the date field is empty.
   - starts in state `payment_form`; click `button_now`; effect **read**; timeout 10000 ms
8. `s08_type`: Enter the payment amount on the target invoice's row only.
   - starts in state `payment_form`; type `${amount}` into `textbox_payment_amount`; effect **none**; timeout 10000 ms
   - verify: target `textbox_payment_amount` equals input `amount` (exact comparison)
9. `s09_click`: Submit the payment form, which leads to Dolibarr's confirmation step.
   - starts in state `payment_form`; click `button_pay`; effect **read**, navigates; timeout 10000 ms
10. `s10_assert`: The review screen shows the requested <invoice_ref>
   - starts in state `payment_confirm`; assert that target `link_invoice_ref` equals input `invoice_ref` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
11. `s11_assert`: The review screen shows the requested <amount>
   - starts in state `payment_confirm`; assert that target `textbox_payment_amount` equals input `amount` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
12. `s12_assert`: The review screen shows the requested <bank_account>
   - starts in state `payment_confirm`; assert that target `combobox_select_selectaccountid` equals input `bank_account` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
13. `s13_click`: Confirm the requested payment (Yes is selected); this commits the payment.
   - starts in state `payment_confirm`; click `button_validate`; effect **commit**, navigates; timeout 10000 ms
   - verify: all of: target `cell_amount` equals input `amount` (text comparison); target `cell_payments` is found exactly once
14. `s14_assert`: The screen shows the requested <amount>
   - starts in state `invoice_card`; assert that target `cell_amount` equals input `amount` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms
15. `s15_extract`: Record the reference of the newly created payment on this invoice.
   - starts in state `invoice_card`; read `cell_payments` into output `payment_ref`; effect **read**; timeout 10000 ms
16. `s16_click`: Open the created payment's card, where the ref sits in a field with a stable label.
   - starts in state `invoice_card`; click `link_payments`; effect **read**, navigates; timeout 10000 ms
17. `s17_assert`: The screen shows the requested <method>
   - starts in state `payment_card`; assert that target `cell_method` equals input `method` (text comparison) (otherwise IDENTITY_MISMATCH); effect **none**; timeout 10000 ms

Commit steps: `s13_click`

Requests the profile treats as commits:

- GET * query action=confirm_valid query confirm=yes
- GET * query action=confirm_*
- POST /compta/paiement.php body action=confirm_paiement body confirm=yes

## Targets

### `link_invoice_ref`

role link "${invoice_ref}"

- primary: role link "${invoice_ref}"  (role and exact name)
- fallback 1: role link "[REDACTED:input]"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fingerprint: role=link, tag=a, row_label=[REDACTED:input]

### `link_enter_payment`

role link "Enter payment"

- primary: role link "Enter payment"  (role and exact name)
- fallback 1: role link "enter payment"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fingerprint: role=link, tag=a

### `combobox_select_selectpaiementcode`

css select#selectpaiementcode

- primary: css select#selectpaiementcode  (stable attributes (structural fallback))
- fallback 1: css select.selectpaymenttypes  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 2: css select[name="paiementcode"]  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=combobox, tag=select, row_label=Payment method

### `combobox_select_selectaccountid`

css select#selectaccountid

- primary: css select#selectaccountid  (stable attributes (structural fallback))
- fallback 1: css select.selectbankaccount  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 2: css select.widthcentpercentminusx  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 3: css select[name="accountid"]  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=combobox, tag=select, row_label=Account to credit

### `cell_bank_account`

role cell "${bank_account}"

- primary: role cell "${bank_account}"  (role and exact name)
- fallback 1: value in the row labelled "Account to credit"  (the row's label cell leads to it); used only if it agrees with the fingerprint
- fallback 2: role cell "[REDACTED:input]"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fingerprint: role=cell, tag=td, row_label=Account to credit

### `button_now`

role button "Now"

- primary: role button "Now"  (role and exact name)
- fallback 1: role button "now"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fallback 2: css button#reButtonNow  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 3: css button[name="_useless"]  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=button, tag=button, row_label=Date

### `textbox_payment_amount`

textbox in column "Payment amount" of the row keyed "${invoice_ref}"

- primary: textbox in column "Payment amount" of the row keyed "${invoice_ref}"  (its column header, in the row keyed by the input)
- fingerprint: role=textbox, tag=input, input_type=text, column_header=Payment amount

### `button_pay`

role button "Pay"

- primary: role button "Pay"  (role and exact name)
- fallback 1: role button "pay"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fallback 2: css input.button  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 3: css input.reposition  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=button, tag=input, input_type=submit

### `button_validate`

role button "Validate"

- primary: role button "Validate"  (role and exact name)
- fallback 1: control in the row labelled "Do you confirm this payment input for 50 Euros? All invoices with no remainder to pay will be automatically closed with status "Paid"."  (the row's label cell leads to it); used only if it agrees with the fingerprint
- fallback 2: role button "validate"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fallback 3: css input.button  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 4: css input.confirmvalidatebutton  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 5: css input.small  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 6: css input.valignmiddle  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=button, tag=input, input_type=submit, row_label=Do you confirm this payment input for 50 Euros? All invoices with no remainder to pay will be automatically closed with status "Paid"., column_header=Payments received from customers

### `cell_amount`

cell in column "Amount" of the row keyed "${method}"

- primary: cell in column "Amount" of the row keyed "${method}"  (its column header, in the row keyed by the input)
- fingerprint: role=cell, tag=td, column_header=Amount

### `cell_payments`

cell in column "Payments" of the row keyed "${method}"

- primary: cell in column "Payments" of the row keyed "${method}"  (its column header, in the row keyed by the input)
- fingerprint: role=cell, tag=td, column_header=Payments

### `link_payments`

link in column "Payments" of the row keyed "${amount}"

- primary: link in column "Payments" of the row keyed "${amount}"  (its column header, in the row keyed by the input)
- fallback 1: link in column "Payments" of the row keyed "${method}"  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 2: link in column "Payments" of the only row  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 3: link within columnheader "Payments"  (scoped by the container it sits in); used only if it agrees with the fingerprint
- fingerprint: role=link, tag=a, column_header=Payments

### `cell_method`

role cell "${method}"

- primary: role cell "${method}"  (role and exact name)
- fallback 1: value in the row labelled "Payment method"  (the row's label cell leads to it); used only if it agrees with the fingerprint
- fallback 2: role cell "[REDACTED:input]"  (normalized name (low rank)); used only if it agrees with the fingerprint
- fingerprint: role=cell, tag=td, row_label=Payment method

### `generic_div_marginbottomonly`

css div.marginbottomonly

- primary: css div.marginbottomonly  (stable attributes (structural fallback))
- fallback 1: css div.refid  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fallback 2: css div.refidpadding  (stable attributes (structural fallback)); used only if it agrees with the fingerprint
- fingerprint: role=generic, tag=div

### `cell_invoice`

cell in column "Invoice" of the row keyed "${amount}"

- primary: cell in column "Invoice" of the row keyed "${amount}"  (its column header, in the row keyed by the input)
- fallback 1: cell in column "Invoice" of the row keyed "${invoice_ref}"  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 2: cell in column "Invoice" of the only row  (its column header, in the row keyed by the input); used only if it agrees with the fingerprint
- fallback 3: cell within columnheader "Invoice"  (scoped by the container it sits in); used only if it agrees with the fingerprint
- fingerprint: role=cell, tag=td, row_label=[REDACTED:input], column_header=Invoice

### `cell_amount_2`

role cell "${amount} €"

- primary: role cell "${amount} €"  (role and exact name)
- fallback 1: value in the row labelled "Amount"  (the row's label cell leads to it); used only if it agrees with the fingerprint
- fingerprint: role=cell, tag=td, row_label=Amount

### `cell_bank_account_2`

value in the row labelled "Bank account"

- primary: value in the row labelled "Bank account"  (the row's label cell leads to it)
- fingerprint: role=cell, tag=td, row_label=Bank account

## Success checkpoint

- ends in state `payment_card` (or a specialization of it)
- outputs: `payment_ref`
- check: target `generic_div_marginbottomonly` is found exactly once
- check: target `cell_invoice` is found exactly once
- check: target `cell_method` is found exactly once
- check: target `cell_amount_2` is found exactly once
- check: target `cell_bank_account_2` is found exactly once

## States used

- `home`: Home dashboard. (origin observed; layer page)
  - recognised when all of: the URL matches `/index.php*`; some text equals "Global view"
- `invoice_not_found`: Invoice list whose filter matched nothing: the table body says 'No record found'. (origin observed; layer page; specializes `invoice_list`; default meaning: outcome `invoice_not_found`)
  - recognised when all of: the URL matches `/compta/facture/list.php*`; some text (cell) matches /Customer invoices( \(\d+\))?/; some text (cell) equals "No record found"
- `invoice_list`: Customer invoice list (any filter). (origin observed; layer page)
  - recognised when all of: the URL matches `/compta/facture/list.php*`; some text (cell) matches /Customer invoices( \(\d+\))?/
- `invoice_card`: Customer invoice card (any status). (origin observed; layer page)
  - recognised when all of: the URL matches `/compta/facture/card.php*`; some text (link) equals "Customer invoice"
- `payment_form`: Payment entry form for a customer invoice. (origin observed; layer page)
  - recognised when all of: the URL matches `/compta/paiement.php*`; some text (cell) equals "Enter payment received from customer"
- `payment_confirm`: Payment form after Pay: Dolibarr asks 'Do you confirm this payment input' (an inline Yes/No select and a Validate button, not a dialog). Submitting it posts the payment. (origin observed; layer page; specializes `payment_form`)
  - recognised when all of: the URL matches `/compta/paiement.php*`; some text (cell) equals "Enter payment received from customer"; some text (cell) matches /Do you confirm this payment input for .*/
- `payment_card`: Customer invoice payment card (the recorded payment: ref, amount, bank account). (origin observed; layer page)
  - recognised when all of: the URL matches `/compta/paiement/card.php*`; some text (link) equals "Payment"; some text (cell) equals "Bank account"
- `login`: Login form. Shown at whatever URL was requested once the session is gone, so it is recognised by its fields, never by URL. (origin observed; layer page; default meaning: failure `SESSION_EXPIRED`)
  - recognised when all of: some text (textbox) equals "Login"; some text (textbox) equals "Password"; some text (button) equals "Login"
