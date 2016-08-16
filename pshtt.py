#!/usr/bin/env python

import requests
import re
import datetime
from time import strptime
import base64
import json
import csv
import os
import utils
import logging
import requests_cache

from models import Domain, Endpoint

from sslyze.server_connectivity import ServerConnectivityInfo
from sslyze.plugins.certificate_info_plugin import CertificateInfoPlugin
from sslyze.plugins.hsts_plugin import HstsPlugin

# whether/where to cache, set via --cache
WEB_CACHE = None

# Default, overrideable via --user-agent
USER_AGENT = "pshtt, https scanning"

# Defaults to 1 second, overrideable via --timeout
TIMEOUT = 1

# The fields we're collecting, will be keys in JSON and
# column headers in CSV.
HEADERS = [
    "Domain", "Live", "Redirect",
    "Valid HTTPS", "Defaults HTTPS", "Downgrades HTTPS",
    "Strictly Forces HTTPS", "HTTPS Bad Chain", "HTTPS Bad Host Name",
    "Expired Cert", "Weak Signature Chain", "HSTS", "HSTS Header",
    "HSTS Max Age", "HSTS All Subdomains", "HSTS Preload",
    "HSTS Preload Ready", "HSTS Preloaded"
]

PRELOAD_CACHE = None
preload_list = None


def inspect(base_domain):
    domain = Domain(base_domain)
    domain.http = Endpoint("http", "root", base_domain)
    domain.httpwww = Endpoint("http", "www", base_domain)
    domain.https = Endpoint("https", "root", base_domain)
    domain.httpswww = Endpoint("https", "www", base_domain)

    # Analyze HTTP endpoint responsiveness and behavior.
    basic_check(domain.http)
    basic_check(domain.httpwww)
    basic_check(domain.https)
    basic_check(domain.httpswww)

    # Analyze HSTS header, if present, on each HTTPS endpoint.
    hsts_check(domain.https)
    hsts_check(domain.httpswww)

    # TODO: move this into basic_check for HTTPS endpoints
    if domain.https.live:
        https_check(domain.https)
    if domain.httpswww.live:
        https_check(domain.httpswww)

    return result_for(domain)


def result_for(domain):

    # print(utils.json_for(domain.to_object()))

    # TODO:
    # Because it will inform many other judgments, first identify
    # an acceptable "canonical" URL for the domain.
    # canonical = canonical_endpoint(http, httpwww, https, httpswww)

    # First, the basic fields the CSV will use.
    result = {
        'Domain': domain.domain,
        'Live': is_live(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Redirect': is_redirect(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Valid HTTPS': is_valid_https(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Defaults HTTPS': is_defaults_to_https(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Downgrades HTTPS': is_downgrades_https(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Strictly Forces HTTPS': is_strictly_forces_https(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HTTPS Bad Chain': is_bad_chain(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HTTPS Bad Host Name': is_bad_hostname(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'Expired Cert': is_expired_cert(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS': is_hsts(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS Header': hsts_header(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS Max Age': hsts_max_age(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS All Subdomains': is_hsts_all_subdomains(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS Preload': is_hsts_preload(domain.http, domain.httpwww, domain.https, domain.httpswww),
        'HSTS Preload Ready': is_hsts_preload_ready(domain.http, domain.httpwww, domain.https, domain.httpswww),

        # Doesn't use endpoint behavior.
        'HSTS Preloaded': is_hsts_preloaded(domain)
    }

    # But also capture the extended data for those who want it.
    result['endpoints'] = {
        'http': domain.http.to_object(),
        'httpwww': domain.httpwww.to_object(),
        'https': domain.https.to_object(),
        'httpswww': domain.httpswww.to_object()
    }

    return result


def basic_check(endpoint):
    logging.debug("pinging %s..." % endpoint.url)

    # First check if the endpoint is live
    try:
        r = requests.get(
            endpoint.url,
            data={'User-Agent': USER_AGENT},
            timeout=TIMEOUT
        )
        # If status code starts with a 3, it is a redirect
        if len(r.history) > 0 and str(r.history[0].status_code).startswith('3'):
            endpoint.redirect = True

            # TODO: handle relative redirects (where Location header omits origin)
            endpoint.redirect_immediately_to = r.history[0].headers['Location']
            endpoint.redirect_to = r.url
        endpoint.live = True

        # Store original headers and status code.
        if len(r.history) > 0:
            endpoint.headers = dict(r.history[0].headers)
            endpoint.status = r.history[0].status_code
        else:
            endpoint.headers = dict(r.headers)
            endpoint.status = r.status_code


    # The endpoint is live but there is a bad cert
    except requests.exceptions.SSLError:
        # TODO: this is too broad, won't always be chain error.
        endpoint.https_bad_chain = True
        endpoint.live = True
        # If there is a bad cert and the domain is not an https endpoint it is a redirect
        if endpoint.protocol == "http":
            endpoint.redirect = True
    # Endpoint is not live
    # TODO: Too broad, shouldn't swallow all errors.
    # except:
    #     logging.debug("Endpoint is not live: %s" % endpoint.url)
    #     pass


def https_check(endpoint):
    logging.debug("sslyzing %s..." % endpoint.url)

    # Use sslyze to check for HSTS and certificate errors
    try:
        # remove the https:// from prefix for sslyze
        hostname = endpoint.url[8:]
        server_info = ServerConnectivityInfo(hostname=hostname, port=443)
        server_info.test_connectivity_to_server()

        # Call plugin directly
        cert_plugin = CertificateInfoPlugin()
        cert_plugin_result = cert_plugin.process_task(server_info, 'certinfo_basic')
        # Parsing sslyze output for results by line
        for i in cert_plugin_result.as_text():
            # Check for cert expiration
            if "Not After" in i:
                expired_cert(i, endpoint)
            # Check for Hostname validation
            elif "Hostname Validation" in i:
                bad_hostname(i, endpoint)
            # Check if Cert is trusted based on CA Stores
            elif "CA Store" in i:
                bad_chain(i, endpoint)
                break
    except:
        # No valid hsts
        pass


# Given an endpoint and its detected headers, extract and parse
# any present HSTS header, decide what HSTS properties are there.
def hsts_check(endpoint):
    header = endpoint.headers.get("Strict-Transport-Security")

    if header is None:
        endpoint.hsts = False
        return

    endpoint.hsts = True
    endpoint.hsts_header = header

    # Set max age to the string after max-age
    temp = header.split()
    endpoint.hsts_max_age = temp[0][len("max-age="):]

    # check if hsts includes sub domains
    if 'includesubdomains' in header.lower():
        endpoint.hsts_all_subdomains = True

    # Check is hsts has the preload flag
    if 'preload' in header.lower():
        endpoint.hsts_preload = True


def bad_chain(trusted, endpoint):
    # If the cert is not trusted by mozilla it is a bad chain
    if "FAILED" in trusted:
        endpoint.https_bad_chain = True


def bad_hostname(hostname_validation, endpoint):
    # If hostname validation fails
    if "FAILED" in hostname_validation:
        endpoint.https_bad_hostname = True


def expired_cert(expired_date, endpoint):
    # Split the time into an list of subtrings
    temp = expired_date.split()
    # Convert the date returned by sslyze to be comparable to current time
    if datetime.datetime(int(temp[5]), strptime(temp[2], '%b').tm_mon, int(temp[3])) < datetime.datetime.now():
        endpoint.https_expired_cert = True


##
# Given behavior for the 4 endpoints, make a best guess
# as to which is the "canonical" site for the domain.
##
def canonical_endpoint(http, httpwww, https, httpswww):
    pass

##
# Judgment calls based on observed endpoint data.
##


# Domain is live if *any* endpoint is live.
def is_live(http, httpwww, https, httpswww):
    return http.live or httpwww.live or https.live or httpswww.live


# TODO: Loosen this definition to check if:
# at least one endpoint is a redirect, and
# all endpoints are either redirects or down.
def is_redirect(http, httpwww, https, httpswww):
    return http.redirect or httpwww.redirect or https.redirect or httpswww.redirect


def is_valid_https(http, httpwww, https, httpswww):
    # One of the HTTPS endpoints has to be up,
    # and has to have a cert for a valid hostname,
    # and has to not downgrade the user to HTTP (either doesn't redirect, or if it does redirect it stays at HTTPS).
    # TODO: only evaluate canonical endpoint.

    def supports_https(endpoint):
        return endpoint.live and \
            (not endpoint.https_bad_hostname) and \
            (not (endpoint.redirect and (endpoint.redirect_immediately_to[:5] != "https")))

    return supports_https(https) or supports_https(httpswww)


# Domain defaults to https if http endpoint forwards to https
def is_defaults_to_https(http, httpwww, https, httpswww):
    if http.redirect or httpwww.redirect:
        return (http.redirect and (http.redirect_to[:5] == "https")) or (httpwww.redirect and (httpwww.redirect_to[:5] == "https"))
    else:
        return False


# Domain downgrades if https endpoint redirects to http
def is_downgrades_https(http, httpwww, https, httpswww):
    return (https.redirect and (https.redirect_to[:5] == "http:")) or (httpswww.redirect and (httpswww.redirect_to[:5] == "http:"))


# A domain strictly forces https if https is live and http is not,
# if both http forward to https endpoints or if one http forwards to https and the other is not live
def is_strictly_forces_https(http, httpwww, https, httpswww):
    if ((not http.live) and (not httpwww.live)) and (https.live or httpswww.live):
        return True
    elif (http.redirect and (http.redirect_to[:5] == "https")) and (httpwww.redirect and (httpwww.redirect_to[:5] == "https")):
        return True
    elif (http.redirect and (http.redirect_to[:5] == "https")) and (not httpwww.live):
        return True
    elif (httpwww.redirect and (httpwww.redirect_to[:5] == "https")) and (not http.live):
        return True
    else:
        return False


# Domain has a bad chain if either https endpoints contain a bad chain
def is_bad_chain(http, httpwww, https, httpswww):
    return https.https_bad_chain or httpswww.https_bad_chain


# Domain has a bad hostname if either https endpoint fails hostname validation
def is_bad_hostname(http, httpwww, https, httpswww):
    return https.https_bad_hostname or httpswww.https_bad_hostname


# Domain has hsts ONLY if the https (and not the www subdomain) has strict transport in the header
def is_hsts(http, httpwww, https, httpswww):
    return https.hsts


def hsts_header(http, httpwww, https, httpswww):
    if https.hsts:
        return https.hsts_header
    else:
        return None


def hsts_max_age(http, httpwww, https, httpswww):
    if https.hsts:
        return https.hsts_max_age
    else:
        return None


def is_hsts_all_subdomains(http, httpwww, https, httpswww):
    # Returns if the https endpoint has "includesubdomains"
    return https.hsts_all_subdomains


def is_hsts_preload_ready(http, httpwww, https, httpswww):
    # returns if the hsts header exists, has a max age, includes subdomains, and includes preload
    return (https.hsts and https.hsts_max_age != "" and https.hsts_all_subdomains and https.hsts_preload)


def is_hsts_preload(http, httpwww, https, httpswww):
    # Returns if https endpoint has preload in hsts header
    return https.hsts_preload


def is_expired_cert(http, httpwww, https, httpswww):
    # Returns if the either https endpoint has an expired cert
    return https.https_expired_cert or httpswww.https_expired_cert


def is_hsts_preloaded(domain):
    # Returns if a domain is on the Chromium preload list
    return domain in preload_list


def create_preload_list():
    preload_json = None

    if PRELOAD_CACHE and os.path.exists(PRELOAD_CACHE):
        logging.debug("Using cached Chrome preload list.")
        preload_json = json.loads(open(PRELOAD_CACHE).read())
    else:
        logging.debug("Fetching Chrome preload list from source...")

        # Downloads the chromium preloaded domain list and sets it to a global set
        file_url = 'https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json?format=TEXT'

        # TODO: proper try/except around this network request
        request = requests.get(file_url)
        raw = request.content

        # To avoid parsing the contents of the file out of the source tree viewer's
        # HTML, we download it as a raw file. googlesource.com Base64-encodes the
        # file to avoid potential content injection issues, so we need to decode it
        # before using it. https://code.google.com/p/gitiles/issues/detail?id=7
        raw = base64.b64decode(raw).decode('utf-8')

        # The .json file contains '//' comments, which are not actually valid JSON,
        # and confuse Python's JSON decoder. Begone, foul comments!
        raw = ''.join([re.sub(r'^\s*//.*$', '', line)
                       for line in raw.splitlines()])

        preload_json = json.loads(raw)

        if PRELOAD_CACHE:
            logging.debug("Caching preload list at %s" % PRELOAD_CACHE)
            utils.write(utils.json_for(preload_json), PRELOAD_CACHE)

    # For our purposes, we only care about entries that includeSubDomains
    fully_preloaded = []
    for entry in preload_json['entries']:
        if entry.get('include_subdomains', False) is True:
            fully_preloaded.append(entry['name'])

    return fully_preloaded


# Output a CSV string for an array of results, with a
# header row, and with header fields in the desired order.
def csv_for(results, out_filename):
    out_file = open(out_filename, 'w')
    writer = csv.writer(out_file)

    writer.writerow(HEADERS)

    for result in results:
        row = []
        for header in HEADERS:
            row.append(result[header])
        writer.writerow(row)

    out_file.close()


def inspect_domains(domains, options):
    # Override timeout, user agent, preload cache.
    global TIMEOUT, USER_AGENT, PRELOAD_CACHE, WEB_CACHE
    if options.get('timeout'):
        TIMEOUT = int(options['timeout'])
    if options.get('user_agent'):
        USER_AGENT = options['user_agent']
    if options.get('preload_cache'):
        PRELOAD_CACHE = options['preload_cache']
    if options.get('cache'):
        cache_dir = ".cache"
        utils.mkdir_p(cache_dir)
        requests_cache.install_cache("%s/cache" % cache_dir)

    # Download HSTS preload list, caches locally.
    global preload_list
    preload_list = create_preload_list()

    # For every given domain, get inspect data.
    results = []
    for domain in domains:
        results.append(inspect(domain))

    return results