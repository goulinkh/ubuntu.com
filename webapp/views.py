# Standard library
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import re

# Packages
import dateutil.parser
import feedparser
import flask
import gnupg
import pytz
import talisker.requests
import yaml
import jinja2
from ubuntu_release_info.data import Data
from canonicalwebteam.store_api.stores.snapstore import SnapStore
from canonicalwebteam.launchpad import Launchpad
from geolite2 import geolite2
from requests.exceptions import HTTPError
from canonicalwebteam.search.models import get_search_results
from canonicalwebteam.search.views import NoAPIKeyError


# Local
from webapp.login import empty_session, user_info
from webapp.advantage import (
    AdvantageContracts,
    UnauthorizedError,
    CannotCancelLastContractError,
)
from webapp.decorators import store_maintenance


ip_reader = geolite2.reader()
session = talisker.requests.get_session()
store_api = SnapStore(session=talisker.requests.get_session())


def _build_mirror_list():
    # Build mirror list
    mirrors = []
    mirror_list = []

    try:
        with open(f"{os.getcwd()}/etc/ubuntu-mirrors-rss.xml") as rss:
            mirrors = feedparser.parse(rss.read()).entries
    except IOError:
        pass

    country_code = "NO_COUNTRY_CODE"
    ip_location = ip_reader.get(
        flask.request.headers.get("X-Real-IP", flask.request.remote_addr)
    )

    if ip_location and "country" in ip_location:
        country_code = ip_location["country"]["iso_code"]

        for mirror in mirrors:
            if mirror["mirror_countrycode"] == country_code and mirror[
                "link"
            ].startswith("https"):
                mirror_list.append(
                    {
                        "link": mirror["link"],
                        "bandwidth": mirror["mirror_bandwidth"],
                    }
                )

    return mirror_list


def show_template(filename):
    try:
        template_content = flask.render_template(f"templates/{filename}.html")
    except jinja2.exceptions.TemplateNotFound:
        flask.abort(404)

    return (
        template_content,
        {"Access-Control-Allow-Origin": "*"},
    )


def download_server_steps():
    templates = {
        "step1": "download/server/step1.html",
        "step2": "download/server/step2.html",
        "choose": "download/server/choose.html",
        "download": "download/server/download.html",
    }
    context = {}
    step = flask.request.form.get("next-step") or "step1"

    if step not in templates:
        flask.abort(400)

    if step == "download":
        version = flask.request.form.get("version")

        if not version:
            flask.abort(400)

        context = {"version": version, "mirror_list": _build_mirror_list()}

    return flask.render_template(templates[step], **context)


def download_thank_you(category):
    version = flask.request.args.get("version", "")
    architecture = flask.request.args.get("architecture", "").replace(" ", "+")

    if version and not architecture:
        flask.abort(400)

    return (
        flask.render_template(
            f"download/{category}/thank-you.html",
            version=version,
            architecture=architecture,
            mirror_list=_build_mirror_list(),
        ),
        {"Cache-Control": "no-cache"},
    )


def appliance_install(appliance, device):
    with open("appliances.yaml") as appliances:
        appliances = yaml.load(appliances, Loader=yaml.FullLoader)

    return flask.render_template(
        f"appliance/{appliance}/{device}.html",
        http_host=flask.request.host,
        appliance=appliances["appliances"][appliance],
    )


def appliance_portfolio():
    with open("appliances.yaml") as appliances:
        appliances = yaml.load(appliances, Loader=yaml.FullLoader)

    return flask.render_template(
        "appliance/portfolio.html",
        http_host=flask.request.host,
        appliances=appliances["appliances"],
    )


def releasenotes_redirect():
    """
    View to redirect to https://wiki.ubuntu.com/ URLs for release notes.

    This used to be done in the Apache frontend, but that is going away
    to be replace by the content-cache.

    Old apache redirects: https://pastebin.canonical.com/p/3TXyyNkWkg/
    """

    version = flask.request.args.get("ver", "")[:5]

    for codename, release in Data().releases.items():
        short_version = ".".join(release.version.split(".")[:2])
        if version == short_version:
            release_slug = release.full_codename.replace(" ", "")

            return flask.redirect(
                f"https://wiki.ubuntu.com/{release_slug}/ReleaseNotes"
            )

    return flask.redirect("https://wiki.ubuntu.com/Releases")


def build():
    """
    Show build page
    """

    return flask.render_template(
        "core/build/index.html",
        board_architectures=json.dumps(Launchpad.board_architectures),
    )


def post_build():
    """
    Once they submit the build form on /core/build,
    kick off the build with Launchpad
    """

    opt_in = flask.request.values.get("canonicalUpdatesOptIn")
    full_name = flask.request.values.get("FullName")
    names = full_name.split(" ")
    email = flask.request.values.get("Email")
    board = flask.request.values.get("board")
    system = flask.request.values.get("system")
    snaps = flask.request.values.get("snaps", "").split(",")
    arch = flask.request.values.get("arch")

    if not user_info(flask.session):
        flask.abort(401)

    launchpad = Launchpad(
        username=os.environ["LAUNCHPAD_IMAGE_BUILD_USER"],
        token=os.environ["LAUNCHPAD_IMAGE_BUILD_TOKEN"],
        secret=os.environ["LAUNCHPAD_IMAGE_BUILD_SECRET"],
        session=session,
        auth_consumer=os.environ["LAUNCHPAD_IMAGE_BUILD_AUTH_CONSUMER"],
    )

    context = {}

    # Submit user to marketo
    session.post(
        "https://pages.ubuntu.com/index.php/leadCapture/save",
        data={
            "canonicalUpdatesOptIn": opt_in,
            "FirstName": " ".join(names[:-1]),
            "LastName": names[-1] if len(names) > 1 else "",
            "Email": email,
            "formid": "3546",
            "lpId": "2154",
            "subId": "30",
            "munchkinId": "066-EOV-335",
            "imageBuilderStatus": "NULL",
        },
    )

    # Ensure webhook is created
    if flask.request.host == "ubuntu.com":
        launchpad.create_update_system_build_webhook(
            system=system,
            delivery_url="https://ubuntu.com/core/build/notify",
            secret=flask.current_app.config["SECRET_KEY"],
        )

    # Kick off image build
    try:
        response = launchpad.build_image(
            board=board,
            system=system,
            snaps=snaps,
            author_info={"name": full_name, "email": email, "board": board},
            gpg_passphrase=flask.current_app.config["SECRET_KEY"],
            arch=arch,
        )
        context["build_info"] = launchpad.session.get(
            response.headers["Location"]
        ).json()
    except HTTPError as http_error:
        if http_error.response.status_code == 400:
            return (
                flask.render_template(
                    "core/build/error.html",
                    build_error=http_error.response.content.decode(),
                ),
                400,
            )
        else:
            raise http_error

    return flask.render_template("core/build/index.html", **context)


def notify_build():
    """
    An endpoint to trigger an update about a build event to be sent.
    This will usually be triggered by a webhook from Launchpad
    """

    # Verify contents
    signature = hmac.new(
        flask.current_app.config["SECRET_KEY"].encode("utf-8"),
        flask.request.data,
        hashlib.sha1,
    ).hexdigest()

    if "X-Hub-Signature" not in flask.request.headers:
        return "No X-Hub-Signature provided\n", 403

    if not hmac.compare_digest(
        signature, flask.request.headers["X-Hub-Signature"].split("=")[1]
    ):
        try:
            raise HTTPError(400)
        except HTTPError:
            flask.current_app.extensions["sentry"].captureException(
                extra={
                    "request_headers": str(flask.request.headers.keys()),
                    "message": "x-hub-signature did not match",
                    "expected_signature": signature,
                    "header_contents": flask.request.headers[
                        "X-Hub-Signature"
                    ],
                    "extracted_signature": flask.request.headers[
                        "X-Hub-Signature"
                    ].split("=")[1],
                }
            )

        return "X-Hub-Signature does not match\n", 400

    event_content = flask.request.json
    status = event_content["status"]
    build_url = (
        "https://api.launchpad.net/devel" + event_content["livefs_build"]
    )

    launchpad = Launchpad(
        username=os.environ["LAUNCHPAD_IMAGE_BUILD_USER"],
        token=os.environ["LAUNCHPAD_IMAGE_BUILD_TOKEN"],
        secret=os.environ["LAUNCHPAD_IMAGE_BUILD_SECRET"],
        session=session,
        auth_consumer=os.environ["LAUNCHPAD_IMAGE_BUILD_AUTH_CONSUMER"],
    )

    build = launchpad.request(build_url).json()
    author_json = (
        gnupg.GPG()
        .decrypt(
            build["metadata_override"]["_author_data"],
            passphrase=flask.current_app.config["SECRET_KEY"],
        )
        .data
    )

    if author_json:
        author = json.loads(author_json)
    else:
        return "_author_data could not be decoded\n", 400

    email = author["email"]
    names = author["name"].split(" ")
    board = author["board"]
    snaps = ", ".join(build["metadata_override"]["extra_snaps"])
    codename = build["distro_series_link"].split("/")[-1]
    version = Data().by_codename(codename).version
    arch = build["distro_arch_series_link"].split("/")[-1]
    build_link = build["web_link"]
    build_id = build_link.split("/")[-1]

    download_url = None

    if status == "Successfully built":
        download_url = launchpad.request(
            f"{build_url}?ws.op=getFileUrls"
        ).json()[0]

    session.post(
        "https://pages.ubuntu.com/index.php/leadCapture/save",
        data={
            "FirstName": " ".join(names[:-1]),
            "LastName": names[-1] if len(names) > 1 else "",
            "Email": email,
            "formid": "3546",
            "lpId": "2154",
            "subId": "30",
            "munchkinId": "066-EOV-335",
            "imageBuilderVersion": version,
            "imageBuilderArchitecture": arch,
            "imageBuilderBoard": board,
            "imageBuilderSnaps": snaps,
            "imageBuilderID": build_id,
            "imageBuilderBuildlink": build_link,
            "imageBuilderStatus": status,
            "imageBuilderDownloadlink": download_url,
        },
    )

    return "Submitted\n", 202


def search_snaps():
    """
    A JSON endpoint to search the snap store API
    """

    query = flask.request.args.get("q", "")
    architecture = flask.request.args.get("architecture", "wide")
    board = flask.request.args.get("board")
    system = flask.request.args.get("system")
    size = flask.request.args.get("size", "100")
    page = flask.request.args.get("page", "1")

    if board and system:
        architecture = Launchpad.board_architectures[board][system]["arch"]

    if not query:
        return flask.jsonify({"error": "Query parameter 'q' empty"}), 400

    search_response = store_api.search(
        query, size=size, page=page, arch=architecture
    )

    return flask.jsonify(
        {
            "results": search_response.get("_embedded", {}).get(
                "clickindex:package", {}
            ),
            "architecture": architecture,
        }
    )


@store_maintenance
def advantage_view():
    is_test_backend = flask.request.args.get("test_backend", False)

    stripe_publishable_key = os.getenv(
        "STRIPE_LIVE_PUBLISHABLE_KEY", "pk_live_68aXqowUeX574aGsVck8eiIE"
    )

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        stripe_publishable_key = os.getenv(
            "STRIPE_TEST_PUBLISHABLE_KEY",
            "pk_test_yndN9H0GcJffPe0W58Nm64cM00riYG4N46",
        )
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if not user_info(flask.session):
        return flask.render_template(
            "advantage/index-no-login.html",
            is_test_backend=is_test_backend,
        )

    open_subscription = flask.request.args.get("subscription", None)

    personal_account = None
    new_subscription_id = None
    new_subscription_start_date = None
    payment_method_warning = None

    enterprise_contracts = {}
    previous_purchase_ids = {"monthly": "", "yearly": ""}
    monthly_info = {
        "total_subscriptions": 0,
        "has_monthly": False,
        "next_payment": {},
    }

    advantage = AdvantageContracts(
        session,
        flask.session["authentication_token"],
        api_url=api_url,
    )

    try:
        accounts = advantage.get_accounts()
    except HTTPError as http_error:
        if http_error.response.status_code == 401:
            # We got an unauthorized request, so we likely
            # need to re-login to refresh the macaroon
            flask.current_app.extensions["sentry"].captureException(
                extra={
                    "session_keys": flask.session.keys(),
                    "request_url": http_error.request.url,
                    "request_headers": http_error.request.headers,
                    "response_headers": http_error.response.headers,
                    "response_body": http_error.response.json(),
                    "response_code": http_error.response.json()["code"],
                    "response_message": http_error.response.json()["message"],
                }
            )

            empty_session(flask.session)

            return flask.render_template("advantage/index.html")

        raise http_error

    for account in accounts:
        monthly_purchased_products = {}
        yearly_purchased_products = {}
        account["contracts"] = advantage.get_account_contracts(account)

        try:
            all_subscriptions = (
                advantage.get_account_subscriptions_for_marketplace(
                    account_id=account["id"],
                    marketplace="canonical-ua",
                )
            )
        except HTTPError:
            flask.current_app.extensions["sentry"].captureException(
                extra={"account_id": account["id"]}
            )
            return (
                flask.jsonify(
                    {"error": "could not retrieve account subscriptions"}
                ),
                500,
            )

        monthly_subscriptions = []
        yearly_subscriptions = []
        for subscription in all_subscriptions.get("subscriptions", []):
            period = subscription["subscription"]["period"]
            status = subscription["subscription"]["status"]

            # If there are any pending purchase, for monthly (active or locked)
            # we show the payment method warning.
            if period == "monthly" and status in ["active", "locked"]:
                payment_method_warning = subscription.get("pendingPurchases")

            previous_purchase_ids[period] = subscription["lastPurchaseID"]

            if subscription["subscription"]["period"] == "yearly":
                yearly_subscriptions.append(subscription)
                continue

            monthly_subscriptions.append(subscription)

        for subscription in monthly_subscriptions:
            purchased_products = subscription["purchasedProductListings"]
            for purchased_product_listing in purchased_products:
                product_listing = purchased_product_listing["productListing"]
                product_id = product_listing["productID"]
                quantity = purchased_product_listing["value"]
                monthly_purchased_products[product_id] = {
                    "quantity": quantity,
                    "price": product_listing["price"],
                }

            prepare_monthly_info(monthly_info, subscription, advantage)

        for subscription in yearly_subscriptions:
            purchased_products = subscription["purchasedProductListings"]
            for purchased_product_listing in purchased_products:
                product_listing = purchased_product_listing["productListing"]
                product_id = product_listing["productID"]
                quantity = purchased_product_listing["value"]
                yearly_purchased_products[product_id] = {
                    "quantity": quantity,
                    "price": product_listing["price"],
                }

        for contract in account["contracts"]:
            try:
                contract["token"] = advantage.get_contract_token(contract)
            except HTTPError:
                flask.current_app.extensions["sentry"].captureException(
                    extra={"contract_id": contract["contractInfo"]["id"]}
                )
                return (
                    flask.jsonify(
                        {"error": "could not retrieve contract token"}
                    ),
                    500,
                )

            contract["machineCount"] = get_machine_usage(advantage, contract)

            if contract["contractInfo"].get("origin", "") == "free":
                personal_account = account
                personal_account["free_token"] = contract["token"]

                continue

            contract_info = contract["contractInfo"]
            entitlements = {}
            for entitlement in contract_info["resourceEntitlements"]:
                contract["supportLevel"] = "-"
                if entitlement["type"] == "support":
                    affordance = entitlement["affordances"]
                    contract["supportLevel"] = affordance["supportLevel"]

                    continue

                entitlement_type = entitlement["type"]
                entitlements[entitlement_type] = True
            contract["entitlements"] = entitlements

            created_at = dateutil.parser.parse(contract_info["createdAt"])
            format_create = created_at.strftime("%d %B %Y")
            contract["contractInfo"]["createdAtFormatted"] = format_create
            contract["contractInfo"]["status"] = "active"

            time_now = datetime.utcnow().replace(tzinfo=pytz.utc)

            if (
                not new_subscription_start_date
                or created_at > new_subscription_start_date
            ):
                new_subscription_start_date = created_at
                new_subscription_id = contract["contractInfo"]["id"]

            effective_to = dateutil.parser.parse(contract_info["effectiveTo"])
            format_effective = effective_to.strftime("%d %B %Y")
            contract["contractInfo"]["effectiveToFormatted"] = format_effective

            if effective_to < time_now:
                contract["contractInfo"]["status"] = "expired"
                restart_date = time_now - timedelta(days=1)
                contract["contractInfo"]["expired_restart_date"] = restart_date

            date_difference = effective_to - time_now
            contract["expiring"] = date_difference.days <= 30
            contract["contractInfo"]["daysTillExpiry"] = date_difference.days

            try:
                contract["renewal"] = make_renewal(
                    advantage, contract["contractInfo"]
                )
            except KeyError:
                flask.current_app.extensions["sentry"].captureException()
                contract["renewal"] = None

            enterprise_contract = enterprise_contracts.setdefault(
                contract["accountInfo"]["name"], []
            )

            product_name = contract["contractInfo"]["products"][0]

            contract["productID"] = product_name
            contract["is_detached"] = False
            contract["machineCount"] = "-"

            if product_name in yearly_purchased_products:
                purchased_product = yearly_purchased_products[product_name]
                contract["price_per_unit"] = purchased_product["price"]
                contract["machineCount"] = purchased_product["quantity"]
                contract["period"] = "yearly"

                if contract["contractInfo"]["id"] == open_subscription:
                    enterprise_contract.insert(0, contract)
                elif contract["contractInfo"]["id"] == new_subscription_id:
                    enterprise_contract.insert(0, contract)
                else:
                    enterprise_contract.append(contract)

            if product_name in monthly_purchased_products:
                contract = contract.copy()
                purchased_product = monthly_purchased_products[product_name]
                contract["price_per_unit"] = purchased_product["price"]
                contract["machineCount"] = purchased_product["quantity"]
                contract["is_cancelable"] = True
                contract["period"] = "monthly"

                if contract["contractInfo"]["id"] == open_subscription:
                    enterprise_contract.insert(0, contract)
                elif contract["contractInfo"]["id"] == new_subscription_id:
                    enterprise_contract.insert(0, contract)
                else:
                    enterprise_contract.append(contract)

            if (
                product_name not in yearly_purchased_products
                and product_name not in monthly_purchased_products
            ):
                contract["is_detached"] = True

                if contract["contractInfo"]["id"] == open_subscription:
                    enterprise_contract.insert(0, contract)
                elif contract["contractInfo"]["id"] == new_subscription_id:
                    enterprise_contract.insert(0, contract)
                else:
                    enterprise_contract.append(contract)

    return flask.render_template(
        "advantage/index.html",
        accounts=accounts,
        payment_method_warning=payment_method_warning,
        subscriptions=monthly_info,
        enterprise_contracts=enterprise_contracts,
        previous_purchase_ids=previous_purchase_ids,
        personal_account=personal_account,
        open_subscription=open_subscription,
        new_subscription_id=new_subscription_id,
        stripe_publishable_key=stripe_publishable_key,
        is_test_backend=is_test_backend,
    )


def prepare_monthly_info(monthly_info, subscription, advantage):
    purchased_products = subscription["purchasedProductListings"]
    purchased_products_no = len(purchased_products)

    monthly_info["total_subscriptions"] += purchased_products_no
    monthly_info["has_monthly"] = True
    monthly_info["id"] = subscription["subscription"]["id"]
    monthly_info["is_auto_renewal_enabled"] = subscription.get(
        "autoRenew", False
    )

    last_purchase_id = subscription["lastPurchaseID"]

    try:
        last_purchase = advantage.get_purchase(last_purchase_id)
    except HTTPError:
        flask.current_app.extensions["sentry"].captureException(
            extra={"last_purchase_id": last_purchase_id}
        )
        return (
            flask.jsonify({"error": "could not fetch last purchase"}),
            500,
        )

    monthly_info["last_payment_date"] = dateutil.parser.parse(
        last_purchase["createdAt"]
    ).strftime("%d %B %Y")
    monthly_info["current_subscription_no"] = purchased_products_no
    monthly_info["next_payment"]["date"] = dateutil.parser.parse(
        subscription["subscription"]["endOfCycle"]
    ).strftime("%d %B %Y")
    monthly_info["next_payment"]["ammount"] = get_subscription_payment_total(
        subscription["purchasedProductListings"]
    )


def get_subscription_payment_total(products_listings):
    total = 0

    for listing in products_listings:
        total += listing["productListing"]["price"]["value"] * listing["value"]

    return (
        f"{total / 100} "
        f'{products_listings[0]["productListing"]["price"]["currency"]}'
    )


def get_machine_usage(advantage, contract):
    """Return machine usage for the given contract as a MachineUsage object."""
    allowances = contract.get("contractInfo", {}).get("allowances", [])
    allowed = sum(a["value"] for a in allowances)

    try:
        attached_machines = advantage.get_contract_machines(contract).get(
            "machines", []
        )
    except HTTPError:
        flask.current_app.extensions["sentry"].captureException(
            extra={"contract_id": contract["contractInfo"]["id"]}
        )
        return (
            flask.jsonify({"error": "could not retrieve attached machines"}),
            500,
        )
    attached = len(attached_machines)
    return MachineUsage(attached=attached, allowed=allowed)


class MachineUsage(namedtuple("MachineUsage", ["attached", "allowed"])):
    """Store attached and allowed machine count in a tuple."""

    __slots__ = ()

    def __str__(self):
        if self.allowed:
            return f"{self.attached}/{self.allowed}"
        return str(self.attached)


def post_advantage_subscriptions(preview):
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    user_token = flask.session.get("authentication_token")
    guest_token = flask.session.get("guest_authentication_token")

    if user_info(flask.session) or guest_token:
        advantage = AdvantageContracts(
            session,
            user_token or guest_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )
    else:
        return flask.jsonify({"error": "authentication required"}), 401

    payload = flask.request.json
    if not payload:
        return flask.jsonify({}), 400

    account_id = payload.get("account_id")
    previous_purchase_id = payload.get("previous_purchase_id")
    period = payload.get("subscription_period")
    existing_subscription = {}

    if not guest_token:
        try:
            subscriptions = (
                advantage.get_account_subscriptions_for_marketplace(
                    account_id=account_id,
                    marketplace="canonical-ua",
                    filters={"status": "active"},
                )
            )
        except HTTPError:
            flask.current_app.extensions["sentry"].captureException(
                extra={"payload": payload}
            )
            return (
                flask.jsonify(
                    {"error": "could not retrieve account subscriptions"}
                ),
                500,
            )

        for subscription in subscriptions.get("subscriptions", []):
            if subscription["subscription"]["period"] == period:
                existing_subscription = subscriptions["subscriptions"]

    # If there is a subscription we get the current metric
    # value for each product listing so we can generate a
    # purchase request with updated quantities later.
    subscribed_quantities = {}
    if "purchasedProductListings" in existing_subscription:
        for item in existing_subscription["purchasedProductListings"]:
            product_listing_id = item["productListing"]["id"]
            subscribed_quantities[product_listing_id] = item["value"]

    purchase_items = []
    for product in payload.get("products"):
        product_listing_id = product["product_listing_id"]
        metric_value = product["quantity"] + subscribed_quantities.get(
            product_listing_id, 0
        )

        purchase_items.append(
            {
                "productListingID": product_listing_id,
                "metric": "active-machines",
                "value": metric_value,
            }
        )

    purchase_request = {
        "accountID": account_id,
        "purchaseItems": purchase_items,
        "previousPurchaseID": previous_purchase_id,
    }

    try:
        if not preview:
            purchase = advantage.purchase_from_marketplace(
                marketplace="canonical-ua", purchase_request=purchase_request
            )
        else:
            purchase = advantage.preview_purchase_from_marketplace(
                marketplace="canonical-ua", purchase_request=purchase_request
            )
    except HTTPError as http_error:
        flask.current_app.extensions["sentry"].captureException(
            extra={
                "purchase_request": purchase_request,
                "api_response": http_error.response.json(),
            }
        )
        return (
            flask.jsonify(
                {
                    "purchase_request": purchase_request,
                    "api_response": http_error.response.json(),
                }
            ),
            500,
        )

    return flask.jsonify(purchase), 200


def cancel_advantage_subscriptions():
    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if flask.request.args.get("test_backend", False):
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    user_token = flask.session.get("authentication_token")

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            user_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )
    else:
        return flask.jsonify({"error": "authentication required"}), 401

    payload = flask.request.json

    account_id = payload.get("account_id")
    previous_purchase_id = payload.get("previous_purchase_id")
    product_listings = payload.get("product_listings")

    if not (account_id and previous_purchase_id and product_listings):
        return flask.jsonify({"error": "bad request"}), 400

    account_id = payload.get("account_id")
    previous_purchase_id = payload.get("previous_purchase_id")

    try:
        monthly_subscriptions = (
            advantage.get_account_subscriptions_for_marketplace(
                account_id=account_id,
                marketplace="canonical-ua",
                filters={"status": "active", "period": "monthly"},
            )
        )
    except HTTPError:
        flask.current_app.extensions["sentry"].captureException(
            extra={"payload": payload}
        )
        return (
            flask.jsonify(
                {"error": "could not retrieve account subscriptions"}
            ),
            500,
        )

    if not monthly_subscriptions.get("subscriptions"):
        return flask.jsonify({"error": "no monthly subscriptions found"}), 400

    monthly_subscription = monthly_subscriptions.get("subscriptions")[0]

    purchase_request = {
        "accountID": account_id,
        "purchaseItems": [
            {
                "productListingID": product_listing,
                "metric": "active-machines",
                "value": 0,
                "delete": True,
            }
            for product_listing in product_listings
        ],
        "previousPurchaseID": previous_purchase_id,
    }

    try:
        purchase = advantage.purchase_from_marketplace(
            marketplace="canonical-ua", purchase_request=purchase_request
        )
    except CannotCancelLastContractError:
        try:
            advantage.cancel_subscription(
                subscription_id=monthly_subscription["subscription"]["id"]
            )

            return (
                flask.jsonify({"message": "Subscription Cancelled"}),
                200,
            )
        except HTTPError as http_error:
            flask.current_app.extensions["sentry"].captureException(
                extra={
                    "subscription": monthly_subscription,
                    "api_response": http_error.response.json(),
                }
            )

            return (
                flask.jsonify({"error": "could not cancel subscription"}),
                500,
            )
    except HTTPError as http_error:
        flask.current_app.extensions["sentry"].captureException(
            extra={
                "purchase_request": purchase_request,
                "api_response": http_error.response.json(),
            }
        )

        return flask.jsonify({"error": "purchase failed"}), 500

    return flask.jsonify(purchase), 200


@store_maintenance
def advantage_shop_view():
    account = None
    previous_purchase_ids = {"monthly": "", "yearly": ""}
    is_test_backend = flask.request.args.get("test_backend", False)

    stripe_publishable_key = os.getenv(
        "STRIPE_LIVE_PUBLISHABLE_KEY", "pk_live_68aXqowUeX574aGsVck8eiIE"
    )
    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        stripe_publishable_key = os.getenv(
            "STRIPE_TEST_PUBLISHABLE_KEY",
            "pk_test_yndN9H0GcJffPe0W58Nm64cM00riYG4N46",
        )
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=api_url,
        )
        if flask.session.get("guest_authentication_token"):
            flask.session.pop("guest_authentication_token")

        try:
            account = advantage.get_purchase_account()
        except HTTPError as err:
            code = err.response.status_code
            if code == 401:
                # We got an unauthorized request, so we likely
                # need to re-login to refresh the macaroon
                flask.current_app.extensions["sentry"].captureException(
                    extra={
                        "session_keys": flask.session.keys(),
                        "request_url": err.request.url,
                        "request_headers": err.request.headers,
                        "response_headers": err.response.headers,
                        "response_body": err.response.json(),
                        "response_code": err.response.json()["code"],
                        "response_message": err.response.json()["message"],
                    }
                )

                empty_session(flask.session)

                return flask.render_template(
                    "advantage/subscribe/index.html",
                    account=None,
                    previous_purchase_ids=previous_purchase_ids,
                    product_listings=[],
                    stripe_publishable_key=stripe_publishable_key,
                    is_test_backend=is_test_backend,
                )
            if code != 404:
                raise
            # There is no purchase account yet for this user.
            # One will need to be created later, but this is an expected
            # condition.
    else:
        advantage = AdvantageContracts(session, None, api_url=api_url)

    if account is not None:
        resp = advantage.get_account_subscriptions_for_marketplace(
            account_id=account["id"],
            marketplace="canonical-ua",
            filters={"status": "active"},
        )

        for subscription in resp.get("subscriptions", []):
            period = subscription["subscription"]["period"]
            previous_purchase_ids[period] = subscription["lastPurchaseID"]

    listings_response = advantage.get_marketplace_product_listings(
        "canonical-ua"
    )
    product_listings = listings_response.get("productListings")
    if not product_listings:
        # For the time being, no product listings means the shop has not been
        # activated, so fallback to shopify. This should become an error later.
        return flask.redirect("https://buy.ubuntu.com/")

    products = {pr["id"]: pr for pr in listings_response["products"]}
    listings = []
    for listing in product_listings:
        if "price" not in listing:
            continue
        listing["product"] = products[listing["productID"]]
        listings.append(listing)

    return flask.render_template(
        "advantage/subscribe/index.html",
        account=account,
        previous_purchase_ids=previous_purchase_ids,
        product_listings=listings,
        stripe_publishable_key=stripe_publishable_key,
        is_test_backend=is_test_backend,
    )


@store_maintenance
def advantage_payment_methods_view():
    is_test_backend = flask.request.args.get("test_backend", False)
    default_payment_method = None
    account_id = None

    stripe_publishable_key = os.getenv(
        "STRIPE_LIVE_PUBLISHABLE_KEY", "pk_live_68aXqowUeX574aGsVck8eiIE"
    )

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        stripe_publishable_key = os.getenv(
            "STRIPE_TEST_PUBLISHABLE_KEY",
            "pk_test_yndN9H0GcJffPe0W58Nm64cM00riYG4N46",
        )
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=api_url,
        )

        try:
            account = advantage.get_purchase_account()
            customer_info_response = get_customer_info(account["id"])
            if customer_info_response["success"]:
                customer_info = customer_info_response["data"].get(
                    "customerInfo"
                )

                if customer_info:
                    default_payment_method = customer_info.get(
                        "defaultPaymentMethod"
                    )

                    if customer_info.get("accountInfo"):
                        account_id = customer_info["accountInfo"].get("id")

        except HTTPError as http_error:
            if http_error.response.status_code == 401:
                # We got an unauthorized request, so we likely
                # need to re-login to refresh the macaroon
                flask.current_app.extensions["sentry"].captureException(
                    extra={
                        "session_keys": flask.session.keys(),
                        "request_url": http_error.request.url,
                        "request_headers": http_error.request.headers,
                        "response_headers": http_error.response.headers,
                        "response_body": http_error.response.json(),
                        "response_code": http_error.response.json()["code"],
                        "response_message": http_error.response.json()[
                            "message"
                        ],
                    }
                )

                empty_session(flask.session)

                return flask.render_template("advantage/index.html")

            raise http_error

    return flask.render_template(
        "advantage/payment-methods/index.html",
        stripe_publishable_key=stripe_publishable_key,
        is_test_backend=is_test_backend,
        default_payment_method=default_payment_method,
        account_id=account_id,
    )


@store_maintenance
def advantage_thanks_view():
    email = flask.request.args.get("email")

    if user_info(flask.session):
        return flask.redirect("/advantage")
    else:
        return flask.render_template(
            "advantage/subscribe/thank-you.html",
            email=email,
        )


def make_renewal(advantage, contract_info):
    """Return the renewal as present in the given info, or None."""
    renewals = contract_info.get("renewals")
    if not renewals:
        return None

    sorted_renewals = sorted(
        (r for r in renewals if r["status"] != "closed"),
        key=lambda renewal: dateutil.parser.parse(renewal["start"]),
    )

    if len(sorted_renewals) == 0:
        return None

    renewal = sorted_renewals[0]

    # If the renewal is processing, we need to find out
    # whether payment failed and requires user action,
    # which is information only available in the fuller
    # renewal object get_renewal gives us.
    if renewal["status"] == "processing":
        renewal = advantage.get_renewal(renewal["id"])

    renewal["renewable"] = False

    if renewal["status"] == "done":
        try:
            renewal_modified_date = dateutil.parser.parse(
                renewal["lastModified"]
            )
            oneHourAgo = datetime.now(timezone.utc) - timedelta(hours=1)

            renewal["recently_renewed"] = oneHourAgo < renewal_modified_date
        except KeyError:
            renewal["recently_renewed"] = False

    # Only actionable renewals are renewable.
    # If "actionable" isn't set, it's not actionable
    # If "actionable" IS set, but not true, it's not actionable
    if "actionable" not in renewal:
        renewal["actionable"] = False
        return renewal
    elif not renewal["actionable"]:
        return renewal

    # The renewal is renewable only during its time window.
    start = dateutil.parser.parse(renewal["start"])
    end = dateutil.parser.parse(renewal["end"])
    if not (start <= datetime.now(timezone.utc) <= end):
        return renewal

    # Pending renewals are renewable.
    if renewal["status"] == "pending":
        renewal["renewable"] = True
        return renewal

    # Renewals not pending or processing are never renewable.
    if renewal["status"] != "processing":
        return renewal

    invoices = renewal.get("stripeInvoices")
    if invoices:
        invoice = invoices[-1]
        renewal["renewable"] = (
            invoice["pi_status"] == "requires_payment_method"
            or invoice["pi_status"] == "requires_action"
        ) and invoice["subscription_status"] == "incomplete"

    return renewal


def post_anonymised_customer_info():
    user_token = flask.session.get("authentication_token")
    guest_token = flask.session.get("guest_authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session) or guest_token:
        advantage = AdvantageContracts(
            session,
            user_token or guest_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )

        if not flask.request.is_json:
            return flask.jsonify({"error": "JSON required"}), 400

        account_id = flask.request.json.get("account_id")
        if not account_id:
            return flask.jsonify({"error": "account_id required"}), 400

        address = flask.request.json.get("address")
        if not address:
            return flask.jsonify({"error": "address required"}), 400

        tax_id = flask.request.json.get("tax_id")

        return advantage.put_anonymous_customer_info(
            account_id, address, tax_id
        )
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def post_payment_method():
    user_token = flask.session.get("authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            user_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )

        if not flask.request.is_json:
            return flask.jsonify({"error": "JSON required"}), 400

        account_id = flask.request.json.get("account_id")
        if not account_id:
            return flask.jsonify({"error": "account_id required"}), 400

        payment_method_id = flask.request.json.get("payment_method_id")
        if not payment_method_id:
            return flask.jsonify({"error": "payment_method_id required"}), 400

        try:
            return advantage.put_payment_method(account_id, payment_method_id)
        except HTTPError as http_error:
            flask.current_app.extensions["sentry"].captureException(
                extra={
                    "payment_method_id": payment_method_id,
                    "api_response": http_error.response.json(),
                }
            )
            return (
                flask.jsonify(
                    {"error": "could not update default payment method"}
                ),
                500,
            )
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def post_auto_renewal_settings():
    user_token = flask.session.get("authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if not user_info(flask.session):
        return flask.jsonify({"error": "authentication required"}), 401

    should_auto_renew = flask.request.json.get("should_auto_renew", False)

    if not should_auto_renew:
        return flask.jsonify({"error": "should_auto_renew required"}), 400

    advantage = AdvantageContracts(
        session,
        user_token,
        token_type=("Macaroon" if user_token else "Bearer"),
        api_url=api_url,
    )

    try:
        accounts = advantage.get_accounts()
    except HTTPError:
        flask.current_app.extensions["sentry"].captureException()
        return (
            flask.jsonify({"error": "could not retrieve accounts"}),
            500,
        )

    for account in accounts:
        try:
            monthly_subscriptions = (
                advantage.get_account_subscriptions_for_marketplace(
                    account_id=account["id"],
                    marketplace="canonical-ua",
                    filters={"status": "active", "period": "monthly"},
                )
            )
        except HTTPError:
            flask.current_app.extensions["sentry"].captureException(
                extra={"account_id": account["id"]}
            )
            return (
                flask.jsonify(
                    {"error": "could not retrieve account subscriptions"}
                ),
                500,
            )

        for subscription in monthly_subscriptions.get("subscriptions", []):
            try:
                advantage.post_subscription_auto_renewal(
                    subscription_id=subscription["subscription"]["id"],
                    should_auto_renew=should_auto_renew,
                )
            except HTTPError as http_error:
                flask.current_app.extensions["sentry"].captureException(
                    extra={
                        "subscription_id": subscription["subscription"]["id"],
                        "api_response": http_error.response.json(),
                    }
                )
                return (
                    flask.jsonify(
                        {
                            "error": "could not change auto renewal settings",
                        }
                    ),
                    500,
                )

    return (
        flask.jsonify({"message": "subscription renewal status was changed"}),
        200,
    )


def post_customer_info():
    user_token = flask.session.get("authentication_token")
    guest_token = flask.session.get("guest_authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session) or guest_token:
        advantage = AdvantageContracts(
            session,
            user_token or guest_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )

        if not flask.request.is_json:
            return flask.jsonify({"error": "JSON required"}), 400

        payment_method_id = flask.request.json.get("payment_method_id")
        if not payment_method_id:
            return flask.jsonify({"error": "payment_method_id required"}), 400

        account_id = flask.request.json.get("account_id")
        if not account_id:
            return flask.jsonify({"error": "account_id required"}), 400

        address = flask.request.json.get("address")
        name = flask.request.json.get("name")
        tax_id = flask.request.json.get("tax_id")

        return advantage.put_customer_info(
            account_id, payment_method_id, address, name, tax_id
        )
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def post_stripe_invoice_id(tx_type, tx_id, invoice_id):
    user_token = flask.session.get("authentication_token")
    guest_token = flask.session.get("guest_authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session) or guest_token:
        advantage = AdvantageContracts(
            session,
            user_token or guest_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )

        return advantage.post_stripe_invoice_id(tx_type, tx_id, invoice_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def get_purchase(purchase_id):
    user_token = flask.session.get("authentication_token")
    guest_token = flask.session.get("guest_authentication_token")
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session) or guest_token:
        advantage = AdvantageContracts(
            session,
            user_token or guest_token,
            token_type=("Macaroon" if user_token else "Bearer"),
            api_url=api_url,
        )

        return advantage.get_purchase(purchase_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def ensure_purchase_account():
    """
    Returns an object with the ID of an account a user can make
    purchases on. If the user is not logged in, the object also
    contains an auth token required for subsequent calls to the
    contract API.
    """
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if not flask.request.is_json:
        return flask.jsonify({"error": "JSON required"}), 400

    auth_token = None
    if user_info(flask.session):
        auth_token = flask.session["authentication_token"]
    advantage = AdvantageContracts(
        session,
        auth_token,
        api_url=api_url,
    )

    request = flask.request.json
    try:
        account = advantage.ensure_purchase_account(
            email=request.get("email"),
            account_name=request.get("account_name"),
            payment_method_id=request.get("payment_method_id"),
        )
    except UnauthorizedError as err:
        # This kind of errors are handled js side.
        return err.asdict(), 200
    except HTTPError as err:
        flask.current_app.extensions["sentry"].captureException()
        return err.response.content, 500

    # The guest authentication token is included in the response only when the
    # user is not logged in.
    token = account.get("token")
    if token:
        flask.session["guest_authentication_token"] = token
    return flask.jsonify(account), 200


def get_renewal(renewal_id):
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=api_url,
        )

        return advantage.get_renewal(renewal_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def get_customer_info(account_id):
    is_test_backend = flask.request.args.get("test_backend", False)
    response = {"success": False, "data": {}}

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    try:
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=api_url,
        )
        response["data"] = advantage.get_customer_info(account_id)
        response["success"] = True
    except HTTPError as error:
        if error.response.status_code == 404:
            response["data"] = error.response.json()
            response["success"] = False
        else:
            flask.current_app.extensions["sentry"].captureException()
            raise error
    return response


def accept_renewal(renewal_id):
    is_test_backend = flask.request.args.get("test_backend", False)

    api_url = flask.current_app.config["CONTRACTS_LIVE_API_URL"]

    if is_test_backend:
        api_url = flask.current_app.config["CONTRACTS_TEST_API_URL"]

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=api_url,
        )

        return advantage.accept_renewal(renewal_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def build_tutorials_index(session, tutorials_docs):
    def tutorials_index():
        page = flask.request.args.get("page", default=1, type=int)
        topic = flask.request.args.get("topic", default=None, type=str)
        sort = flask.request.args.get("sort", default=None, type=str)
        query = flask.request.args.get("q", default=None, type=str)
        posts_per_page = 15

        """
        Get search results from Google Custom Search
        """

        # The webteam's default custom search ID
        search_engine_id = "009048213575199080868:i3zoqdwqk8o"

        # API key should always be provided as an environment variable
        search_api_key = os.getenv("SEARCH_API_KEY")

        if query and not search_api_key:
            raise NoAPIKeyError("Unable to search: No API key provided")

        results = None

        if query:
            results = get_search_results(
                session=session,
                api_key=search_api_key,
                search_engine_id=search_engine_id,
                siteSearch="ubuntu.com/tutorials",
                query=query,
            )

        tutorials_docs.parser.parse()
        if not topic:
            metadata = tutorials_docs.parser.metadata
        else:
            metadata = [
                doc
                for doc in tutorials_docs.parser.metadata
                if topic in doc["categories"]
            ]

        if query:
            temp_metadata = []
            if results.get("entries"):
                for result in results["entries"]:
                    start = result["link"].find("tutorials/")
                    end = len(result["link"])
                    identifier = result["link"][start:end]
                    if start != -1:
                        for doc in metadata:
                            if identifier in doc["topic"]:
                                temp_metadata.append(doc)
            metadata = temp_metadata

        if sort == "difficulty-desc":
            metadata = sorted(
                metadata, key=lambda k: k["difficulty"], reverse=True
            )

        if sort == "difficulty-asc" or not sort:
            metadata = sorted(
                metadata, key=lambda k: k["difficulty"], reverse=False
            )

        total_results = len(metadata)
        total_pages = math.ceil(total_results / posts_per_page)

        return flask.render_template(
            "tutorials/index.html",
            navigation=tutorials_docs.parser.navigation,
            forum_url=tutorials_docs.parser.api.base_url,
            metadata=metadata,
            page=page,
            topic=topic,
            sort=sort,
            query=query,
            posts_per_page=posts_per_page,
            total_results=total_results,
            total_pages=total_pages,
        )

    return tutorials_index


def build_engage_index(engage_docs):
    def engage_index():
        page = flask.request.args.get("page", default=1, type=int)
        topic = flask.request.args.get("topic", default=None, type=str)
        sort = flask.request.args.get("sort", default=None, type=str)
        preview = flask.request.args.get("preview")
        posts_per_page = 15
        engage_docs.parser.parse()
        metadata = engage_docs.parser.metadata

        if preview is None:
            metadata = [
                item
                for item in metadata
                if "active" in item and item["active"] == "true"
            ]

        total_pages = math.ceil(len(metadata) / posts_per_page)

        return flask.render_template(
            "engage/index.html",
            forum_url=engage_docs.parser.api.base_url,
            metadata=metadata,
            page=page,
            topic=topic,
            sort=sort,
            preview=preview,
            posts_per_page=posts_per_page,
            total_pages=total_pages,
        )

    return engage_index


def engage_thank_you(engage_pages):
    """
    Renders an engage pages thank-you page
    i.e. whitepapers, pdfs

    If there is no current topic it can't render the page
    e.g. accessing directly

    @parameters: language (optional) and page path name
    e.g. /cloud-init-whitepaper
    @returns: a function that renders a template
    """

    def render_template(language, page):
        engage_pages.parser.parse()
        page_url = f"/engage/{page}"
        if language:
            page_url = f"/engage/{language}/{page}"
        index_topic_data = next(
            (x for x in engage_pages.parser.metadata if x["path"] == page_url),
            None,
        )

        if index_topic_data:
            topic_id = engage_pages.parser.url_map[page_url]
            engage_page_data = engage_pages.parser.get_topic(topic_id)
            request_url = flask.request.referrer
            resource_name = index_topic_data["type"]
            resource_url = engage_page_data["metadata"]["resource_url"]
            language = index_topic_data["language"]
            # Filter related engage pages by language
            related = [
                item
                for item in engage_page_data["related"]
                if item["language"] == language
            ]
            template_language = "engage/thank-you.html"
            if language and language != "en":
                template_language = f"engage/shared/_{language}_thank-you.html"

            return flask.render_template(
                template_language,
                request_url=request_url,
                resource_name=resource_name,
                resource_url=resource_url,
                related=related,
            )
        else:
            return flask.abort(404)

    return render_template


# Blog
# ===
class BlogView(flask.views.View):
    def __init__(self, blog_views):
        self.blog_views = blog_views


class BlogCustomTopic(BlogView):
    def dispatch_request(self, slug):
        page_param = flask.request.args.get("page", default=1, type=int)
        context = self.blog_views.get_topic(slug, page_param)

        return flask.render_template(f"blog/topics/{slug}.html", **context)


class BlogCustomGroup(BlogView):
    def dispatch_request(self, slug):
        page_param = flask.request.args.get("page", default=1, type=int)
        category_param = flask.request.args.get(
            "category", default="", type=str
        )
        context = self.blog_views.get_group(slug, page_param, category_param)

        return flask.render_template(f"blog/{slug}.html", **context)


class BlogPressCentre(BlogView):
    def dispatch_request(self):
        page_param = flask.request.args.get("page", default=1, type=int)
        category_param = flask.request.args.get(
            "category", default="", type=str
        )
        context = self.blog_views.get_group(
            "canonical-announcements", page_param, category_param
        )

        return flask.render_template("blog/press-centre.html", **context)


class BlogSitemapIndex(BlogView):
    def dispatch_request(self):
        response = session.get(
            "https://admin.insights.ubuntu.com/sitemap_index.xml"
        )

        xml = response.text.replace(
            "https://admin.insights.ubuntu.com/",
            "https://ubuntu.com/blog/sitemap/",
        )
        xml = re.sub(r"<\?xml-stylesheet.*\?>", "", xml)

        response = flask.make_response(xml)
        response.headers["Content-Type"] = "application/xml"
        return response


class BlogSitemapPage(BlogView):
    def dispatch_request(self, slug):
        response = session.get(f"https://admin.insights.ubuntu.com/{slug}.xml")

        if response.status_code == 404:
            return flask.abort(404)

        xml = response.text.replace(
            "https://admin.insights.ubuntu.com/", "https://ubuntu.com/blog/"
        )
        xml = re.sub(r"<\?xml-stylesheet.*\?>", "", xml)

        response = flask.make_response(xml)
        response.headers["Content-Type"] = "application/xml"
        return response


def sitemap_index():
    xml_sitemap = flask.render_template("sitemap_index.xml")
    response = flask.make_response(xml_sitemap)

    response.headers["Content-Type"] = "application/xml"
    return response
