from flask import Flask, request, jsonify
import pymongo
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from datetime import timedelta
import datetime
import uuid
from functools import wraps
import os
from location import get_location
from format_files import format_records, format_individual_resource
import requests
import statistics
import sys
from dotenv import load_dotenv
import pandas as pd
import numpy as np
from urllib.parse import quote_plus
import logging
import logging.config
import json
import logging
import certifi

load_dotenv()

MONGO_USERNAME = quote_plus(os.getenv("MONGO_USERNAME"))
MONGO_PASSWORD = quote_plus(os.getenv("MONGO_PASSWORD"))

MONGO_URI = os.getenv("MONGO_URI")
client = pymongo.MongoClient(MONGO_URI)
db = client.get_database("sanjeevika")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['APP_KEY']

practitioners_ref = db['practitioners']
appointments_ref = db['appointments']
users_ref = db['users']

def jwt_authenticate(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('x-access-token')
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = users_ref.find_one({'email': data['email']})
            if not current_user:
                return jsonify({'message': 'User not found!'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token is invalid!'}), 401
        except Exception as e:
            return jsonify({'message': 'An error occurred!', 'error': str(e)}), 500
        return f(*args, **kwargs)
    return decorated

def check_practitioner_exists(provider_id):
    practitioner = practitioners_ref.find_one({"id": provider_id})
    return practitioner is not None

def fetch_slots(provider_id):
    # Fetch all appointments for the given provider
    booked_appointments = appointments_ref.find({"provider_id": provider_id})

    booked_slots = {(appt.get("date"), appt.get("time")) for appt in booked_appointments}

    today = datetime.now()
    available_slots = []
    for day_offset in range(7):  # Next 7 days
        current_date = (today + timedelta(days=day_offset)).date().isoformat()
        for hour in range(9, 17):  # 9 AM to 5 PM
            time = f"{hour}:00"
            if (current_date, time) not in booked_slots:
                available_slots.append({"date": current_date, "time": time})

    return available_slots

def update_slot_availability(provider_id, slot_id, is_available):
    practitioner = practitioners_ref.find_one({"id": provider_id})
    if not practitioner:
        return False

    result = practitioners_ref.update_one(
        {"id": provider_id, "slots.id": slot_id},
        {"$set": {"slots.$.is_available": is_available}}
    )

    return result.modified_count > 0

def fetch_slots(provider_id):
    # Fetch the practitioner's document
    practitioner = practitioners_ref.find_one({"id": provider_id})
    if not practitioner or "slots" not in practitioner:
        return []

    # Filter slots where is_available is True
    available_slots = [
        {"slot_id": slot["id"], "time": slot["time"]}
        for slot in practitioner["slots"]
        if slot.get("is_available")
    ]
    
    return available_slots

@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')

    if not email or not password or not name:
        return jsonify({'message': 'Missing email, password, or name'}), 400

    try:
        # Check if the user already exists
        if db['users'].find_one({"email": email}):
            return jsonify({'message': 'User already exists'}), 400

        # Hash the password and create a new user document
        hashed_password = generate_password_hash(password)
        user_id = str(uuid.uuid4())
        
        db['users'].insert_one({
            'email': email,
            'password': hashed_password,
            'name': name,
            'user_id': user_id
        })

        return jsonify({'message': 'User registered successfully', 'user_id': user_id}), 201
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500
    
@app.route('/signin', methods=['POST'])
def signin():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({'message': 'Missing email or password'}), 400

    try:
        # Fetch the user document
        user_doc = db['users'].find_one({"email": email})
        
        if not user_doc:
            return jsonify({'message': 'User does not exist'}), 404

        # Verify the password
        if not check_password_hash(user_doc['password'], password):
            return jsonify({'message': 'Invalid credentials'}), 401

        # Generate JWT token
        token = jwt.encode({
            'email': email,
            'user_id': user_doc['user_id'],
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        return jsonify({'token': token}), 200
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 500
        
@app.route('/nearby_hospitals', methods=['GET'])
@jwt_authenticate
def nearest_hospitals():
    try:
        # Get user's current location
        lat, long = get_location.get_userlocation()
        
        # Fetch hospital data from MongoDB
        hospitals_cursor = db['hospitals'].find()
        
        hospitals = []
        for doc in hospitals_cursor:
            hospitals.append({
                "uuid": doc.get("uuid"),
                "name": doc.get("name"),
                "lat": float(doc.get("lat")),
                "long": float(doc.get("long")),
                "distance": get_location.cartesian_distance(lat, long, float(doc.get("lat")), float(doc.get("long")))
            })

        # Sort hospitals based on distance and get the nearest three
        nearest_hosps = sorted(hospitals, key=lambda x: x["distance"])[:3]
        
        return jsonify({
            "nearest_hospitals": [
                {
                    "uuid": hospital["uuid"],
                    "name": hospital["name"],
                    "latitude": hospital["lat"],
                    "longitude": hospital["long"],
                }
                for hospital in nearest_hosps
            ]
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400
    
@app.route('/register_hospital', methods=['GET', 'POST'])
@jwt_authenticate
def register_hospital():
    if request.method == "POST":
        data = request.form
        name = data.get('name')
        lat, long = get_location.get_userlocation()

        # Validate input fields
        errors = {
            not name: "Name is missing",
            not lat: "Latitude is missing",
            not long: "Longitude is missing"
        }

        for condition, message in errors.items():
            if condition:
                return {'message': message}, 400

        try:
            hospital_data = {
                'uuid': str(uuid.uuid4()),
                'name': name,
                'lat': float(lat),
                'long': float(long)
            }

            # Insert into MongoDB
            db['hospitals'].insert_one(hospital_data)
            
            return "Hospital registered successfully", 200
        except Exception as e:
            return {'message': f"Error: {str(e)}"}, 500

@app.route('/get_hospitals', methods=['GET'])
@jwt_authenticate
def get_all_hospitals():
    try:
        hospitals_cursor = db['hospitals'].find()
        results = []
        
        for hospital in hospitals_cursor:
            hospital['_id'] = str(hospital['_id'])
            results.append(hospital)

        return jsonify(results), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
@app.route('/add_user', methods=['POST'])
@jwt_authenticate
def add_user():
    data = request.json
    user_id = data.get('user_id')
    name = data.get('name')
    email = data.get('email')

    if not user_id or not name or not email:
        return jsonify({'message': 'Missing user_id, name, or email'}), 400

    try:
        db['user-test1'].insert_one({
            "user_id": user_id,
            "name": name,
            "email": email
        })
        return jsonify({'message': f'User {name} added successfully.'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/add_health_rec', methods=['POST'])
@jwt_authenticate
def add_health_rec():
    try:
        data = request.json
        user_id = data.get('user_id')
        record_data = data.get('record_data')

        if not user_id or not record_data:
            return jsonify({'message': 'Missing user_id or record_data'}), 400

        # Check if the user exists
        user = db['user-test1'].find_one({"user_id": user_id})
        if not user:
            return jsonify({'message': 'User not found'}), 404

        # Generate a unique record ID
        record_id = str(uuid.uuid4())
        record_data['id'] = record_id

        # Insert the health record
        db['user-test1'].update_one(
            {"user_id": user_id},
            {"$push": {"healthRecords": record_data}}
        )

        return jsonify({'message': f'Health record with ID {record_id} added successfully'}), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/get_health_rec/<user_id>', methods=['GET'])
@jwt_authenticate
def get_health_rec(user_id):
    try:
        if not user_id:
            return jsonify({'message': 'Missing user_id'}), 400
        
        # Get optional record type from query params
        record_type = request.args.get('type')
        valid_types = ['Patient', 'Observation', 'Condition', 'Medication', 'Encounter'] 
        
        if record_type and record_type not in valid_types:
            return jsonify({'message': f'Invalid record type: {record_type}. Valid types are: {", ".join(valid_types)}'}), 400

        # Fetch user document
        user = db['user-test1'].find_one({"user_id": user_id})
        if not user or 'healthRecords' not in user:
            return jsonify({'message': 'No records found for this user'}), 404
        
        records = user['healthRecords']

        # Filter records if a specific type is requested
        if record_type:
            filtered_records = []
            for record in records:
                if record.get('resourceType') == 'Bundle':
                    # Filter entries within the bundle by record type
                    for entry in record.get('entry', []):
                        resource = entry.get('resource', {})
                        if resource.get('resourceType') == record_type:
                            filtered_records.append(resource)
                elif record.get('resourceType') == record_type:
                    filtered_records.append(record)

            if not filtered_records:
                return jsonify({'message': f'No records found for type: {record_type}'}), 404
            return jsonify({'records': format_records(filtered_records)}), 200
        
        # Return all records if no specific type is requested
        return jsonify({'records': format_records(records)}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/add_resource', methods=['POST'])
@jwt_authenticate
def add_resource():
    """
    Add any FHIR resource (e.g., Patient, Practitioner, Encounter, etc.)
    """
    try:
        data = request.json
        resource_type = data.get("resourceType")
        if not resource_type:
            return jsonify({'message': 'Missing resourceType'}), 400

        # Prepare collection name and assign a unique ID
        collection_name = resource_type.lower() + 's'  # e.g., 'patients', 'conditions'
        data["user_id"] = str(uuid.uuid4())

        # Insert the document into MongoDB
        db[collection_name].insert_one(data)

        return jsonify({'message': f'{resource_type} added successfully', 'id': data["user_id"]}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get_resource/<resource_type>/<resource_id>', methods=['GET'])
@jwt_authenticate
def get_resource(resource_type, resource_id):
    """
    Retrieve a specific FHIR resource by type and ID.
    """
    try:
        collection_name = resource_type.lower() + 's'  # e.g., 'patients', 'conditions'
        resource = db[collection_name].find_one({"user_id": resource_id})

        if not resource:
            return jsonify({'message': f'{resource_type} not found'}), 404

        # Convert ObjectId to string if present
        resource["user_id"] = str(resource["user_id"])
        return jsonify(resource), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get_resources/<resource_type>', methods=['GET'])
@jwt_authenticate
def get_resources(resource_type):
    """
    Retrieve all resources of a specific type.
    """
    try:
        collection_name = resource_type.lower() + 's'
        resources_cursor = db[collection_name].find()
        resources = []
        
        for resource in resources_cursor:
            resource["user_id"] = str(resource["user_id"])  # Convert ObjectId to string
            resources.append(resource)

        if not resources:
            return jsonify({'message': f'No {resource_type} records found'}), 404

        return jsonify({'resources': resources}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/filter_resources/<resource_type>', methods=['GET'])
@jwt_authenticate
def filter_resources(resource_type):
    """
    Filter resources by a query parameter (e.g., gender, condition code, etc.)
    """
    try:
        collection_name = resource_type.lower() + 's'
        query_field = request.args.get('field')  # Field to filter by
        query_value = request.args.get('value')  # Value to match

        if not query_field or not query_value:
            return jsonify({'message': 'Missing field or value for filtering'}), 400

        # Perform filtering using PyMongo's find
        query = {query_field: query_value}
        resources_cursor = db[collection_name].find(query)
        resources = []

        for resource in resources_cursor:
            resource["user_id"] = str(resource["user_id"])  # Convert ObjectId to string
            resources.append(resource)

        if not resources:
            return jsonify({'message': f'No {resource_type} records match the query'}), 404

        return jsonify({'resources': resources}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/update_health_rec', methods=['POST'])
@jwt_authenticate
def update_health_rec():
    try:
        data = request.json
        user_id = data.get('user_id')
        record_id = data.get('record_id')
        record_data = data.get('record_data')

        if not user_id or not record_id or not record_data:
            return jsonify({'message': 'Missing required fields'}), 400

        # Perform the update using PyMongo
        result = db['user-test1'].update_one(
            {"user_id": user_id, "healthRecords.id": record_id},
            {"$set": {f"healthRecords.$": record_data}}
        )

        if result.modified_count == 0:
            return jsonify({'message': 'No matching health record found or no changes made'}), 404

        return jsonify({'message': f'Health record with ID {record_id} updated successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/delete_health_rec', methods=['DELETE'])
@jwt_authenticate
def delete_health_rec():
    try:
        user_id = request.args.get('user_id')
        record_id = request.args.get('record_id')

        if not user_id or not record_id:
            return jsonify({'message': 'Missing user_id or record_id'}), 400

        # Remove the specific health record from the healthRecords array
        result = db['user-test1'].update_one(
            {"user_id": user_id},
            {"$pull": {"healthRecords": {"id": record_id}}}
        )

        if result.modified_count == 0:
            return jsonify({'message': 'No matching health record found'}), 404

        print(f"Health record with ID: {record_id} deleted successfully..")
        return jsonify({'message': f'Health record with ID {record_id} deleted successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/book', methods=['POST'])
@jwt_authenticate
def book_appointment():
    try:
        data = request.json
        provider_id = data.get('provider_id')
        date = data.get('date')
        time = data.get('time')

        if not provider_id or not date or not time:
            return jsonify({"error": "Missing provider_id, date, or time."}), 400

        # Extract user details from JWT
        token = request.headers.get('x-access-token')
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_email = decoded_token.get('email')

        # Fetch user details from MongoDB
        user_details = db['users'].find_one({"user_id": user_email})
        if not user_details:
            return jsonify({"error": "User not found in database."}), 401

        # Check if practitioner exists
        if not check_practitioner_exists(provider_id):
            return jsonify({"error": "Invalid provider_id"}), 404

        # Check if the slot is already booked
        existing_appointment = db['appointments'].find_one({
            "provider_id": provider_id,
            "date": date,
            "time": time
        })

        if existing_appointment:
            return jsonify({"error": "Slot already booked"}), 400

        # Book the appointment
        appointment = {
            "provider_id": provider_id,
            "date": date,
            "time": time,
            "user_details": {
                "email": user_email,
                "name": user_details.get("name")
            },
            "status": "booked"
        }

        result = db['appointments'].insert_one(appointment)

        return jsonify({"status": "success", "appointment_id": str(result.inserted_id)}), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# # View Slots
# @app.route('/slots/<provider_id>', methods=['GET'])
# @jwt_authenticate
# def get_slots(provider_id):
#     # Check if practitioner exists
#     if not check_practitioner_exists(provider_id):
#         return jsonify({"error": "Invalid provider_id"}), 404

#     # Fetch available slots
#     slots = fetch_slots(provider_id)

#     if not slots:
#         return jsonify({"error": "No available slots found for this provider."}), 404

#     return jsonify({"slots": slots})

# Reschedule Appointment
from bson import ObjectId

@app.route('/reschedule', methods=['POST'])
@jwt_authenticate
def reschedule_appointment():
    try:
        data = request.json
        appointment_id = data.get('appointment_id')
        new_date = data.get('new_date')
        new_time = data.get('new_time')

        if not appointment_id or not new_date or not new_time:
            return jsonify({"error": "Missing appointment_id, new_date, or new_time."}), 400

        # Fetch the appointment
        appointment = db['appointments'].find_one({"user_id": ObjectId(appointment_id)})
        if not appointment:
            return jsonify({"error": "Invalid appointment_id"}), 404

        provider_id = appointment.get("provider_id")

        # Check if the new slot is already booked
        existing_appointment = db['appointments'].find_one({
            "provider_id": provider_id,
            "date": new_date,
            "time": new_time
        })
        if existing_appointment:
            return jsonify({"error": "New slot not available"}), 400

        # Update the appointment
        db['appointments'].update_one(
            {"user_id": ObjectId(appointment_id)},
            {"$set": {"date": new_date, "time": new_time, "status": "rescheduled"}}
        )

        return jsonify({"status": "success", "message": "Appointment rescheduled successfully."}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Cancel Appointment
@app.route('/cancel_appointment', methods=['POST'])
@jwt_authenticate
def cancel_appointment():
    try:
        data = request.json
        appointment_id = data.get('appointment_id')

        if not appointment_id:
            return jsonify({"error": "Missing appointment_id."}), 400

        # Fetch the appointment
        appointment = db['appointments'].find_one({"user_id": ObjectId(appointment_id)})
        if not appointment:
            return jsonify({"error": "Invalid appointment_id"}), 404

        # Update appointment status to 'canceled'
        db['appointments'].update_one(
            {"user_id": ObjectId(appointment_id)},
            {"$set": {"status": "canceled"}}
        )

        return jsonify({"status": "success", "message": "Appointment canceled successfully."}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)