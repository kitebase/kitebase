from kitebase.i18n import register_translations

register_translations('it', {
    # db endpoint — CRUD
    'Table name is required':           'Nome tabella obbligatorio',
    "Table '{name}' not found":         "Tabella '{name}' non trovata",
    "Unsupported method: '{method}'":   "Metodo non supportato: '{method}'",
    'Record with id {id} not found':    'Record con id {id} non trovato',
    'No data provided for creation':    'Nessun dato fornito per la creazione',
    'Record created successfully':      'Record creato con successo',
    'Record ID is required for updates':'ID record obbligatorio per l\'aggiornamento',
    'No data provided for update':      'Nessun dato fornito per l\'aggiornamento',
    'Some values are not valid':        'Alcuni valori non sono validi',
    'Not a valid email address':        'Non è un indirizzo email valido',
    'Record updated successfully':      'Record aggiornato con successo',
    'Record ID is required for deletion':'ID record obbligatorio per l\'eliminazione',
    'Record deleted successfully':      'Record eliminato con successo',
    'Query not defined':                'Query non definita',

    # auth
    'Username and password are required': 'Username e password obbligatori',
    'Invalid credentials':              'Credenziali non valide',
    'Account is inactive':              'Account non attivo',
    'User not authenticated':           'Utente non autenticato',
})
