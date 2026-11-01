# Energetický archív pre Home Assistant

Samostatná záloha všetkého, čo je v **Energy paneli** HA: elektrina (odber, dodávka), FVE, batéria, plyn, voda, spotrebiče, náklady a ceny. Údaje sú uložené po hodinách v SQLite mimo HA, každú noc sa zálohujú na NAS a kedykoľvek sa dajú nahrať späť do HA.

- **Prežije zmeny entít.** Veličiny majú stále názvy (`grid_import`, `solar`, `gas`…). Keď v Energy paneli vymeníš senzor, archív to pri ďalšom zbere zistí a históriu nadviaže.
- **Prežije pád databázy HA aj migráciu.** Príkaz `obnov` nahrá históriu do štatistík HA a súčty nadviaže tak, aby v Energy paneli nebol skok.
- **Diery sa dajú doplniť** zo ZSD (15-min elektromer), z Deye Cloud (5-min výkony) alebo ručne z faktúr (voda, plyn).
- Každá hodnota si pamätá **zdroj**. Ktorý vyhrá, určuje priorita v `config.yaml`. Predvolene platí ZSD > HA > Deye > faktúry, pre FVE HA > Deye.

## 1. LXC na Proxmoxe

Debian 12/13, neprivilegovaný kontajner, 1 vCPU, 512 MB RAM, 4 GB disk stačí.

```bash
# v LXC (root)
apt update && apt install -y git
git clone https://github.com/Igypop78/energy-archiv /root/energy-archiv
cd /root/energy-archiv && bash deploy/install.sh
```

## 2. NAS (ANAS, SMB) do LXC

Neprivilegovaný LXC nevie sám pripojiť SMB, preto sa NAS pripojí na **Proxmox hoste** a do LXC sa podá ako bind mount.

Na Proxmox hoste:
```bash
mkdir -p /mnt/anas-energy
cat > /root/.smb-anas <<'EOF'
username=<pouzivatel>
password=<heslo>
EOF
chmod 600 /root/.smb-anas
# uid/gid 100000 = root v neprivilegovanom LXC
echo '//<IP-ANAS>/<share>/energy-archiv /mnt/anas-energy cifs credentials=/root/.smb-anas,uid=100000,gid=100000,iocharset=utf8,_netdev,nofail 0 0' >> /etc/fstab
mount /mnt/anas-energy
pct set <CTID> -mp0 /mnt/anas-energy,mp=/mnt/nas/energy-archiv
pct reboot <CTID>
```
Ak na ANAS zapneš NFS, postup je rovnaký, len v `fstab` bude `nfs`.

## 3. Nastavenie

`/etc/energy-archiv/.env`:
- `HA_TOKEN`: v HA otvor profil → Zabezpečenie → Dlhodobé prístupové tokeny,
- `DEYE_EMAIL`, `DEYE_PASSWORD`: prihlásenie do Deye Cloud (appId a secret sú už vyplnené).

Potom:
```bash
energy-archiv sync            # prvý zber – stiahne celú históriu z HA
energy-archiv status          # veličiny, entity, rozsah dát
energy-archiv deye-stanice    # overí prístup do Deye Cloud
energy-archiv backup          # prvá záloha na NAS
```
Web: `http://<IP-LXC>:8765`. Nočný zber a záloha bežia samé o 3:17 (`systemctl list-timers`).

## 4. Doplnenie histórie

**ZSD**: na portáli exportuj „Profilové dáta – namerané hodnoty“ pre odber aj dodávku (po mesiacoch alebo rokoch) a „Registrové dáta“. Nahraj ich cez web (Import) alebo:
```bash
energy-archiv import-zsd export_*.xls
energy-archiv check-zsd       # súčet profilu vs. odpočty registrov za každý mesiac
```
Typ profilu sa rozpozná sám („dodávka“ → `grid_export`, inak `grid_import`). Pri prechode na zimný čas sa správne spracuje aj opakovaná hodina (ZSD ju označuje `02:00:01`).

**Deye Cloud**:
```bash
energy-archiv import-deye --od 2024-01-01 --do 2026-10-31
```
Za každý deň sa stiahnu 5-min výkony a premenia na hodinové kWh. Ak je deň pokrytý aspoň z 80 %, hodinové hodnoty sa doladia na denný súčet Deye. Mapovanie polí (FVE, nákup, predaj, batéria) je v `config.yaml` → `deye.map`.

**Voda, plyn z faktúr** (CSV alebo text vo webe):
```
velicina;obdobie;hodnota
gas;2025-03;85.2
water;2025-01-01..2025-03-31;12.5
grid_import;2025-10-26 14:00;1.25
```
Mesačný alebo obdobný súčet sa rozdelí rovnomerne len na hodiny, pre ktoré nemá údaj žiadny iný zdroj. Hodnota s konkrétnou hodinou je ručná oprava s najvyššou prioritou. Jednotka musí sedieť s veličinou (plyn v m³ alebo kWh podľa HA, `energy-archiv status`).

```bash
energy-archiv pokrytie gas    # mesiac po mesiaci: súčet, hodiny, zdroje
```

## 5. Obnova do HA

**Najprv urob zálohu HA.** Potom:
```bash
energy-archiv obnov grid_import                      # náhľad: obdobie, zdroje, súčet, nadviazanie
energy-archiv obnov grid_import --apply              # zápis do aktuálnej entity
energy-archiv obnov gas --od 2025-01-01 --do 2025-06-30 --apply
energy-archiv obnov solar --target sensor.nova_fve --apply   # história na novú entitu
```
Ide to aj cez web: Prehľad → veličina → Obnoviť do HA.

- **Pád databázy alebo nový HA:** po prvom spustení nového HA spusti `sync`, aby si archív načítal nové entity, a potom `obnov` pre každú veličinu.
- **Nová entita:** keď nový senzor vymeníš v Energy paneli, pri najbližšom `sync` sa nadviaže sám. `obnov <veličina>` potom nahrá celú starú históriu pod novú entitu.
- Ak archív entitu nespozná (napr. spotrebič s novým menom): `energy-archiv mapuj device_xyz sensor.novy --od "2026-11-01 00:00"`.

## Súbory

| Cesta | Obsah |
|---|---|
| `/etc/energy-archiv/config.yaml` | nastavenia |
| `/etc/energy-archiv/.env` | tokeny a heslá |
| `/var/lib/energy-archiv/archiv.sqlite` | archív |
| NAS `archiv-RRRR-MM-DD.sqlite` | denné a mesačné zálohy |
| NAS `archiv-posledny.csv.gz` | všetko v CSV (čitateľné aj bez nástroja) |

Testy: `python3 tests/test_all.py [priečinok so vzorkami ZSD v CSV]` (simulovaný HA).
