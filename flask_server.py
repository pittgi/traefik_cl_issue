from flask import Flask, request

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def print_headers():
    headers = request.headers
    data = request.data
    print("New request")
    print(f"Headers: {headers}\nData: {data}")
    return "hello"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
