-- Adds the 'process' service check type (process running + listening on given ports; e.g. DHCP-only dnsmasq).
alter type public.check_type add value if not exists 'process';
