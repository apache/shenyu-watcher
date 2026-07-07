/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.shenyu.env;

import org.apache.commons.io.IOUtils;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * @author sinsy
 * @date 2022-09-08
 */
public class CheckEnv {

    private final static String REQUIRED_PYTHON_VERSION = "3.8";

    public static boolean PYTHON_CHECK = false;
    
    private static final Pattern pattern = Pattern.compile("Python (\\d+\\.\\d+\\.\\d+)");

    static {
        String exe = "python -V";
        Process process;
        try {
            process = Runtime.getRuntime().exec(exe);
            InputStream inputStream = process.getInputStream();
            String str = IOUtils.toString(inputStream, StandardCharsets.UTF_8);
            
            Matcher matcher = pattern.matcher(str);
            if (matcher.find()) {
                System.out.println(matcher.group(1));
                PYTHON_CHECK = isVersionGreaterOrEqual(matcher.group(1), REQUIRED_PYTHON_VERSION);
            }
            
            if (!PYTHON_CHECK) {
                exe = "python3 -V";
            }
            process = Runtime.getRuntime().exec(exe);
            inputStream = process.getInputStream();
            str = IOUtils.toString(inputStream, StandardCharsets.UTF_8);
            
            matcher = pattern.matcher(str);
            if (matcher.find()) {
                System.out.println(matcher.group(1));
                PYTHON_CHECK = isVersionGreaterOrEqual(matcher.group(1), REQUIRED_PYTHON_VERSION);
            }

        } catch (IOException e) {
            e.printStackTrace();
        }

    }
    private static boolean isVersionGreaterOrEqual(String version, String requiredVersion) {
        String[] versionParts = version.split("\\.");
        String[] requiredVersionParts = requiredVersion.split("\\.");
        for (int i = 0; i < requiredVersionParts.length; i++) {
            int v = Integer.parseInt(versionParts[i]);
            int rv = Integer.parseInt(requiredVersionParts[i]);
            if (v > rv) {
                return true;
            } else if (v < rv) {
                return false;
            }
        }
        return true;
    }

}
