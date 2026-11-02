#!/bin/bash

# Le chart produit déjà un dump cohérent et son marqueur de fin.
prepare_backup() {
    local marker lot
    [[ "$LDAP_MAX_DUMP_AGE_MINUTES" =~ ^[1-9][0-9]*$ ]]

    marker=$(find /source -maxdepth 1 -type f -name 'openldap-cron-*.sha256' \
        -mmin "-$LDAP_MAX_DUMP_AGE_MINUTES" -printf '%f\n' | sort | tail -n 1)
    if [[ -z "$marker" ]]; then
        echo 'Aucun dump LDAP terminé assez récent ; vérifier le job natif.' >&2
        return 1
    fi

    lot=${marker%.sha256}
    (
        cd /source || exit 1
        sha256sum --check --status "$marker"
        gzip -t "$lot-config.gz" "$lot-data.gz"
    )
    cp -- "/source/$lot-config.gz" "/source/$lot-data.gz" "${stage:?}/"
}
