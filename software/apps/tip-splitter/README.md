# Tips and bill splitting

Calculate a tip and split the rounded total into shares that add up exactly.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Uses decimal half-up rounding; each share is explicitly returned and sums exactly to the total. The default is two currency decimal places; select zero or three where appropriate. Labels do not perform currency conversion.
