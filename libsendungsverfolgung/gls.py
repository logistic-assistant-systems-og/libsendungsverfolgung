import datetime
import decimal
import itertools
import json
import operator
import re
import requests
import time

from . import base
from .events import *

class Location(base.Location):

    def __init__(self, data):
        return super(Location, self).__init__(city=data["city"], country_code=data["countryCode"])

class Store(base.Store):

    DAYS_OF_WEEK = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

    HERE_APP_ID = "s0Ej52VXrLa6AUJEenti"
    HERE_APP_CODE = "mZr-2hFt2fPzaqrCxN0MuA"
    HERE_LAYER_ID = "GLS_PSHOPS_PRD"

    @classmethod
    def _parse_opening_hours(cls, data):
        result = []
        current_day = []

        for part in data.split("|"):
            match = re.match("^Annual closing: (\d\d\/\d\d/\d{4}) - (\d\d/\d\d/\d{4})", part)
            if match:
                part = part[len(match.group(0)):]
                continue # FIXME

            match = re.match("^([A-Z][a-z])\.(?: - ([A-Z][a-z])\.)?: ", part)
            if match:
                if current_day:
                    result[-1] += ",".join(current_day)
                    current_day = []

                day_start = cls.DAYS_OF_WEEK.index(match.group(1))
                if match.group(2):
                    day_end = cls.DAYS_OF_WEEK.index(match.group(2))
                    result.append("%s-%s " % (cls.DAYS_OF_WEEK[day_start], cls.DAYS_OF_WEEK[day_end]))
                else:
                    result.append("%s " % cls.DAYS_OF_WEEK[day_start])
                part = part[len(match.group(0)):]

            if part == "#--:-- - --:--":
                result.pop()
            elif part.startswith("Indleveringstid:"): # Pickup time in Dansih??? IDK
                pass
            else:
                match = re.match("^#(\d\d:\d\d) - (\d\d:\d\d)$", part)
                if not match:
                    raise ValueError("Unable to parse opening hours")
                current_day.append("%s-%s" % match.groups())

        if current_day:
            result[-1] += ",".join(current_day)

        return "; ".join(result)

    @classmethod
    def from_id(cls, store_id):
        params = {
            "app_id": cls.HERE_APP_ID,
            "app_code": cls.HERE_APP_CODE,
            "layer_id": cls.HERE_LAYER_ID,
            "limit": "1",
            "filter": "NAME3=='%s'" % store_id,
            "callback": "Request.JSONP.request_map.request_0"
        }
        r = requests.get(
            "https://cle.api.here.com/2/search/all.json",
            params=params,
            timeout=base.TIMEOUT)

        data = r.text[(len(params["callback"])+1):-1]
        data = json.loads(data)
        if len(data.get("geometries", [])) != 1:
            return None
        data = data["geometries"][0]["attributes"]

        opening_hours = cls._parse_opening_hours(data["DESCRIPTION"])

        return cls(
            name=data["NAME1"],
            address=data["STREET"],
            postcode=data["ZIP"],
            city=data["CITY"],
            country_code=data["COUNTRY"],
            phone=data.get("PHONE"),
            fax=data.get("FAX"),
            email=data.get("EMAIL"),
            opening_hours=opening_hours
        )

class Parcel(base.Parcel):

    COMPANY_IDENTIFIER = "gls"
    COMPANY_NAME = "General Logistics Systems"
    COMPANY_SHORTNAME = "GLS"

    def __init__(self, tracking_number, postcode=None, *args, **kwargs):
        tracking_number = str(tracking_number)

        self._tracking_number = None
        self._tracking_code = None
        if re.match("^[0-9]{11}$", tracking_number):
            self._tracking_number = tracking_number + str(self.check_digit(tracking_number))
        elif re.match("^[0-9]{12}$", tracking_number):
            self._tracking_number = tracking_number
        elif re.match("^[A-Z0-9]{8}$", tracking_number):
            self._tracking_code = tracking_number
        else:
            self._tracking_code = tracking_number
        self._data = None

        self.postcode = postcode

        super(Parcel, self).__init__(*args, **kwargs)

    @classmethod
    def from_barcode(cls, barcode):
        barcode=str(barcode)

        if len(barcode) == 123:
            track_id = barcode[33:41]
            return cls(track_id)

        match = re.match("^([0-9]{11})([0-9])$", barcode)
        if match:
            if str(cls.check_digit(match.group(1))) == match.group(2):
                return cls(barcode)

    def fetch_data(self):
        if self._data:
            return

        params = {
            "caller": "witt002",
            "match": self._tracking_number or self._tracking_code,
            "milis": int(time.time() * 1000)
        }
        r = requests.get(
            "https://gls-group.eu/app/service/open/rest/EU/en/rstt001",
            params=params,
            timeout=base.TIMEOUT)

        if r.status_code == 404:
            raise base.UnknownParcelException()

        self._data = r.json()
        if not "tuStatus" in self._data:
            raise base.UnknownParcelException()

    def _get_info(self, key):
        for info in self._data["tuStatus"][0]["infos"]:
            if info["type"] == key:
                return info["value"]

    @property
    def weight(self):
        self.fetch_data()
        weight = self._get_info("WEIGHT")
        if weight:
            if weight[-3:] == " kg":
                return decimal.Decimal(weight[:-3])
            elif weight[-26:] == " #Missing TextValue: 25197":
                return decimal.Decimal(weight[:-26])

    @property
    def tracking_number(self):
        if not self._tracking_number:
            self.fetch_data()
            tn = self._data["tuStatus"][0]["tuNo"]
            return tn + str(self.check_digit(tn))

        return self._tracking_number

    @property
    def tracking_link(self):
        return "https://gls-group.eu/EU/en/parcel-tracking?match=" + self.tracking_number

    @property
    def product(self):
        """
        Returns the product name

        See chapter 3.2 in
        https://gls-group.eu/DE/media/downloads/GLS_Uni-Box_TechDoku_2D_V0110_01-10-2012_DE-download-4424.pdf
        """
        if self._data:
            return self._get_info("PRODUCT")


        product_id = int(self.tracking_number[2:4])

        if product_id in range(10, 68):
            return "Business-Parcel"
        elif product_id == 71:
            return "Cash-Service (+DAC)"
        elif product_id == 72:
            return "Cash-Service+Exchange-Service"
        elif product_id == 74:
            return "DeliveryAtWork-Service"
        elif product_id == 75:
            return "Guaranteed 24-Service"
        elif product_id == 76:
            return "ShopReturn-Service"
        elif product_id == 78:
            return "Intercompany-Service"
        elif product_id == 85:
            return "Express-Parcel"
        elif product_id == 87:
            return "Exchange-Service Hintransport"
        elif product_id == 89:
            return "Pick&Return/Ship"
        else:
            # Not explicitly mentiond in the docs, but apparently just a regular parcel
            return "Business-Parcel"

    @property
    def is_cash_on_delivery(self):
        product_id = int(self.tracking_number[2:4])
        return product_id in (71, 72)

    @property
    def is_courier_pickup(self):
        product_id = int(self.tracking_number[2:4])
        return product_id == 89

    @property
    def is_parcelshop_return(self):
        product_id = int(self.tracking_number[2:4])
        return product_id == 76

    @property
    def is_express(self):
        product_id = int(self.tracking_number[2:4])
        return product_id == 85

    @property
    def recipient(self):
        self.fetch_data()
        if "signature" in self._data["tuStatus"][0]:
            return self._data["tuStatus"][0]["signature"]["value"]

    @property
    def references(self):
        self.fetch_data()
        references = {}
        for info in self._data["tuStatus"][0]["references"]:
            if info["type"] == "GLSREF":
                if info["name"] == "Origin National Reference in Unicode":
                    references["customer_id"] = info["value"]
                elif info["name"] == "Reference number created via device: Smartphone.":
                    references["paketshop_qr_uuid"] = info["value"]
            elif info["type"] == "CUSTREF":
                if info["name"] == "Customer's own reference number":
                    references["shipment"] = info["value"]
                elif info["name"] == "Customers own reference number - per TU":
                    references["parcel"] = info["value"]
        return references

    def fetch_recipient(self, postcode):
        params = {
            "caller": "witt002",
            "milis": str(int(time.time() * 1000)),
        }
        data = {
            "postalCode": postcode
        }
        r = requests.post("https://gls-group.eu/app/service/open/rest/DE/de/rstt018/" + self.tracking_number[:11], params=params, json=data)

        data = r.json()
        if "signature" in data and "value" in data["signature"]:
            return data["signature"]["value"]

    def extract_paketshop_location(self):
        if "parcelShop" in self._data["tuStatus"][0]:
            return Store.from_id(self._data["tuStatus"][0]["parcelShop"].get("psID"))

    @property
    def events(self):
        self.fetch_data()
        if not "history" in self._data["tuStatus"][0]:
            return []

        events = []
        first_scan = None

        for event in reversed(self._data["tuStatus"][0]["history"]):
            descr = event["evtDscr"]
            when = datetime.datetime.strptime(event["date"] + event["time"], "%Y-%m-%d%H:%M:%S")
            location = Location(event["address"])

            if descr == "The parcel has been delivered.":
                recipient = self.fetch_recipient(self.postcode) if self.postcode else None
                pe = DeliveryEvent(
                    when=when,
                    location=location,
                    recipient=recipient
                )
            elif descr == "The parcel has been delivered at the neighbour\u00b4s (see signature)":
                recipient = self.fetch_recipient(self.postcode) if self.postcode else None
                pe = DeliveryNeighbourEvent(
                    when=when,
                    location=location,
                    recipient=recipient
                )
            elif descr == "The parcel has been delivered / dropped off.":
                pe = DeliveryDropOffEvent(
                    when=when,
                    location=location,
                )
            elif descr == "The parcel is expected to be delivered during the day.":
                pe = InDeliveryEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has reached the ParcelShop.":
                if first_scan is None or first_scan == when.date():
                    pe = PostedEvent(
                        when=when,
                        location=location
                    )
                else:
                    pe = StoreDropoffEvent(
                        when=when,
                        location=self.extract_paketshop_location() or location
                    )
            elif descr == "The parcel was handed over to GLS.":
                pe = InboundSortEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has reached the parcel center.":
                pe = SortEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has reached the parcel center and was sorted manually.":
                pe = ManualSortEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has left the parcel center.":
                pe = OutboundSortEvent(
                    when=when,
                    location=location
                )
            elif descr in ("The parcel could not be delivered as the consignee was absent.",
                           "The parcel could not be delivered as the reception was closed."):
                pe = RecipientUnavailableEvent(
                    when=when,
                    location=location
                )
            elif descr == "The consignee was informed by notification card about the delivery/pickup attempt.":
                pe =  RecipientNotificationEvent(
                    notification="card",
                    when=when,
                    location=location
                )
            elif descr == "The parcel has been delivered at the ParcelShop (see ParcelShop information).":
                pe = StoreDropoffEvent(
                    when=when,
                    location=self.extract_paketshop_location() or location
                )
            elif descr == "The parcel data was entered into the GLS IT system; the parcel was not yet handed over to GLS.":
                pe = DataReceivedEvent(
                    when=when
                )
            elif descr == "Forwarded Redirected" or \
                descr == "The changed delivery option has been saved in the GLS system.":
                pe = RedirectEvent(
                    when=when
                )
            elif descr.startswith("The parcel is stored in the parcel center.") or \
                descr.startswith("The parcel is stored in the final parcel center.") or \
                descr == "The parcel is stored in the parcel center to be delivered at a new delivery date.":
                pe = StoredEvent(
                    when=when,
                    location=location
                )
                if descr.endswith("It could not be delivered as further address information is needed.") or \
                    descr.endswith("It cannot be delivered as further address information is needed."):
                    events.append(WrongAddressEvent(
                        when=when,
                        location=location,
                    ))
                elif descr.endswith("It could not be delivered as the reception was closed."):
                    events.append(RecipientUnavailableEvent(
                        when=when,
                        location=location
                    ))
            elif descr == "The parcel could not be delivered as further address information is needed.":
                pe = WrongAddressEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel could not be delivered as the recipient refused acceptance.":
                pe = DeliveryRefusedEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has reached the maximum storage time in the ParcelShop.":
                pe = StoreNotPickedUpEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has been returned to the shipper.":
                pe = ReturnEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel data have been deleted from the GLS IT system.":
                pe = CancelledEvent(
                    when=when
                )
            elif descr == "The parcel label for the pickup has been produced.":
                pe = ParcelLabelPrintedEvent(
                    when=when,
                    location=location
                )
            elif descr == "The parcel has been picked up by GLS.":
                pe = PickupEvent(
                    when=when,
                    location=location
                )
            else:
                pe = ParcelEvent(
                    when=when
                )

            if isinstance(pe, LocationEvent):
                first_scan = pe.when.date()
            events.append(pe)

        return events

    @staticmethod
    def check_digit(tracking_number):
        """
        Calculates the check digit for the given tracking number.

        See chapter 3.2.1 in
        https://gls-group.eu/DE/media/downloads/GLS_Uni-Box_TechDoku_2D_V0110_01-10-2012_DE-download-4424.pdf
        """
        check_digit = 10 - ((sum(itertools.starmap(operator.mul, zip(itertools.cycle((3, 1)), map(int, str(tracking_number))))) + 1) % 10)
        if check_digit == 10:
            check_digit = 0
        return check_digit
